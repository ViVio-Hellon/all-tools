"""自動同期 : ローカル SQLite と共有の取り込み元を短い間隔で往復させる。

考え方
------
**正式なデータは共有の取り込み元 (sqlite3) だけ**で、ローカルの SQLite は

* 表示を速くするための「読み取り用の写し」
* まだ送れていない変更を貯めておく「送信待ちの置き場 (outbox)」

の 2 つの役割しか持たない。VBA 版は毎回 Access へ直接読み書きしていたが、
ネットワーク越しの往復が遅いためローカルに写しを挟んでいる。

流れ::

    入力 → SQLite に保存 (即座に画面へ反映)
             ↓ 数秒以内に自動で
           取り込み元へ送信 (送れたら outbox から消す)
             ↓ 一定間隔で
           取り込み元から取り込み直す (他ラインの登録が見えるようになる)

送信できなかった場合 (ロック競合・共有フォルダが見えない等) は
outbox に残したまま次回に持ち越すため、入力が失われることはない。

【以前は送信手段が2つありました】
取り込み元が Access だったころは、ODBC 直接接続 (ctypes + odbc32.dll) と
Windows Script Host + ADODB の2段構えで、どちらも使えない端末のために
「反映用ファイルを書き出して別の PC で実行する」手動運用まで持っていた。
取り込み元が sqlite3 になったので、**送信手段は1つだけ**になっている ──
``import sqlite3`` はどの端末にもあるので、使えるかどうかの分岐が要らない。
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Protocol

from .. import config, db, terminals
from . import notices
from ..dbkit import outbox_sync, source_db
from ..logging_utils import debug_log
from . import business_rules, total_pending_count
from . import deletes as sync_deletes
from . import specs as sync_specs

__all__ = [
    "ApplyResult",
    "AutoSync",
    "SyncState",
    "SyncStatus",
    "Transport",
    "SqliteTransport",
    "TransportError",
    "TransportUnavailable",
]


class TransportError(RuntimeError):
    """今回は送れなかった (次回やり直せる)。"""


class TransportUnavailable(TransportError):
    """この環境では取り込み元へ書き込めない。"""


@dataclass
class ApplyResult:
    """取り込み元への反映結果。"""

    applied: int = 0
    skipped: int = 0

    @property
    def total(self) -> int:
        return self.applied + self.skipped


class Transport(Protocol):
    """変更を取り込み元へ届ける手段。"""

    def available(self) -> bool:
        """この環境で使えるか。"""

    def send(self, conn: sqlite3.Connection, source_path: str) -> ApplyResult:
        """SQLite 側の未送信分を取り込み元へ送る。"""


class SqliteTransport:
    """共有フォルダの sqlite3 へ直接書き込む。

    **どの端末でも使える。** ``import sqlite3`` は標準ライブラリなので、
    ドライバの有無で動いたり動かなかったりしない。

    休みの登録だけ ``business_rules`` で業務レベルの重複確認を挟み、
    削除は ``sync/deletes.py`` が別途転送する
    (``dbkit.outbox_sync`` は追加のみ面倒をみるため)。
    """

    def available(self) -> bool:
        return True

    def send(self, conn: sqlite3.Connection, source_path: str) -> ApplyResult:
        if not source_path:
            raise TransportUnavailable(
                "取り込み元が決まっていません。"
                "設定画面の「参照パス」でフォルダを指定してください。")

        try:
            source = source_db.connect(Path(source_path), read_only=False)
        except source_db.SourceError as exc:
            raise TransportError(str(exc)) from exc

        applied = 0
        skipped = 0
        errors: list[str] = []
        try:
            # **削除を先に送る。** 送れない日に「消してから同じ人・同じ日を
            # 登録し直す」と、以前は登録を先に送っていた ── 取り込み元には
            # 消す前の行がまだあるので、新しい登録は「先に登録済み」として
            # 取りやめになり、そのあと削除が届いて**両方とも消えた**。
            # 自然キーで消す削除が、いま送ったばかりの同じ文面の行に
            # 当たることも、先に消せば起きない
            sent_deletes, delete_errors = sync_deletes.forward_pending_deletes(
                conn, source, config.TABLE_DATA)
            applied += sent_deletes
            errors.extend(delete_errors)

            for spec in sync_specs.WRITE_BACK_SPECS:
                if spec.source_table == config.TABLE_DATA:
                    # 休み管理だけ業務レベルの二重登録確認を挟む
                    guarded = business_rules.send_data_with_duplicate_guard(
                        conn, source, spec)
                    applied += guarded.sent
                    skipped += guarded.duplicates
                    errors.extend(guarded.errors)
                    # **黙って取りやめない。** 先に他の端末が登録していた、を
                    # この端末の画面に出す(``sync/notices.py``)
                    notices.remember(conn, guarded.skipped_rows)
                else:
                    result = outbox_sync.write_back(conn, source, [spec])
                    applied += result.total
                    errors.extend(result.errors)
        finally:
            # この端末の設定を置いていく。**送信の成否とは切り離す** ──
            # 見るためのもので、業務データではない。中身が変わったときと
            # 古くなったときしか書かないので、共有への負担は増えない
            try:
                terminals.report(source)
            except Exception as exc:              # noqa: BLE001 - 記録は best effort
                debug_log(f"SqliteTransport.send: 端末設定を記録できず {exc}")
            source.close()

        if errors:
            # 一部でも失敗した行があれば、次回また拾われるよう例外にする
            # (成功した分は既に mark_synced/mark_deletes_sent 済みなので失われない)
            raise TransportError("; ".join(errors))
        return ApplyResult(applied=applied, skipped=skipped)


def _default_transport() -> Transport:
    """使える送信手段。**選択の余地はもう無い。**"""
    return SqliteTransport()


class SyncState(Enum):
    """同期の状態。"""

    SYNCED = "同期済み"
    PENDING = "送信待ち"
    OFFLINE = "取り込み元へ送れません"
    DISABLED = "自動同期オフ"


@dataclass
class SyncStatus:
    """画面に出すための同期状態。"""

    state: SyncState = SyncState.DISABLED
    pending: int = 0
    last_sent_at: str = ""
    last_received_at: str = ""
    message: str = ""

    def describe(self) -> str:
        if self.state is SyncState.SYNCED:
            text = "同期済み"
            if self.last_received_at:
                text += f" ({self.last_received_at})"
            return text
        if self.state is SyncState.PENDING:
            return f"送信待ち {self.pending} 件"
        if self.state is SyncState.OFFLINE:
            base = (f"未送信 {self.pending} 件" if self.pending
                    else "取り込み元へ接続できません")
            return f"{base} / {self.message}" if self.message else base
        return "自動同期オフ"


class AutoSync:
    """送信 (outbox → 取り込み元) と受信 (取り込み元 → SQLite) をまとめて行う。

    スレッドから呼ばれることを前提にしているため、``sqlite3`` の接続は
    このクラスの中で開き直す (接続はスレッドをまたげないため)。
    """

    def __init__(
        self,
        sqlite_path: str | Path,
        source_path: str | Path,
        transport: Transport | None = None,
        master_path: str | Path = "",
    ) -> None:
        self.sqlite_path = str(sqlite_path)
        self.source_path = str(source_path)
        self.transport: Transport = transport or _default_transport()
        #: マスタDB(班員名簿)。**受信のたびに一緒に入れ直す**(``receive``)
        self.master_path = str(master_path or "")
        #: マスタDBにアクセス権限の表があると分かったか(作るのは1度だけ)
        self._access_ready = False
        # 最後に名簿・アクセス権限を入れ直したときのマスタDBの (大きさ, 更新時刻)。
        # 変わっていなければ開かない(下の ``_receive_members``)
        self._master_seen: tuple[int, int] | None = None

    # ------------------------------------------------------------------
    @property
    def enabled(self) -> bool:
        """自動同期が使える状態か。"""
        return bool(self.source_path) and self.transport.available()

    def _connect(self) -> sqlite3.Connection:
        return db.connect(self.sqlite_path)

    # ------------------------------------------------------------------
    # 送信 : outbox にある変更を取り込み元へ
    # ------------------------------------------------------------------
    def send(self, conn: sqlite3.Connection) -> ApplyResult:
        """未送信の変更を取り込み元へ送る。送れた分だけ outbox から消す。

        :raises TransportError: 今回は送れなかった (outbox はそのまま残る)
        """
        result = self.transport.send(conn, self.source_path)
        db.set_meta(conn, "last_sent_at", db.now_string())
        conn.commit()
        debug_log(f"AutoSync.send: 反映={result.applied} 省略={result.skipped}")
        return result

    # ------------------------------------------------------------------
    # 受信 : 取り込み元の内容を取り込み直す
    # ------------------------------------------------------------------
    def receive(self, conn: sqlite3.Connection) -> bool:
        """取り込み元から取り込み直す。未送信の変更がある間は行わない。

        取り込みは対象テーブルを入れ替えるため、送信前に行うと
        まだ届いていない入力が消えてしまう。
        """
        if _pending_count(conn):
            debug_log("AutoSync.receive: 未送信の変更があるため取り込みを見送る")
            return False
        # 総入れ替え後に送信記録を「すべて送信済み」へ揃える処理は
        # import_source 自身が行う (手動取り込みなど他の経路でも
        # 同じ整合性が必要なため、共通化してそちら側に寄せてある)。
        from ..importer import PendingChangesError, import_source

        try:
            import_source(conn, self.source_path)
        except PendingChangesError:
            # **共有を読んでいるあいだに登録が入った。** 入口で0件と確かめた
            # だけでは足りず、そのまま入れ替えると、その登録が消えていた
            # (「打った行が消えることがありました」)。``import_source`` が
            # 錠を取ってから数え直して止めたので、今回は見送る。次の周期で
            # 送ってから取り込み直す
            debug_log("AutoSync.receive: 取り込み中に未送信が増えたため見送る")
            return False
        except sqlite3.OperationalError as exc:
            if not source_db.is_lock_error(exc):
                raise
            # 手元が使用中(登録の最中など)。待てば通るので、次の周期に回す
            debug_log(f"AutoSync.receive: 手元が使用中のため見送る {exc}")
            return False
        self._receive_members(conn)
        db.set_meta(conn, "last_received_at", db.now_string())
        conn.commit()
        return True

    def _receive_members(self, conn: sqlite3.Connection) -> None:
        """班員名簿を**マスタDBから**入れ直す。

        【なぜ要るのか】班員名簿は保存用DBではなくマスタDBにあるので、
        上の ``import_source`` では入りません。以前は設定画面の
        「取り込む」を押すまで入らず、**配布設定で配ったばかりの端末は
        名簿が0件** ── 休みの作業者を選べない状態で始まっていました。
        名簿に足した人が他の端末に出てこない、も同じ理由です。

        班員名簿は書き戻しの無い表なので、送信待ちを気にせず単独で
        入れ直せます(``import_master_table``)。**マスタDBが見えなくても
        受信そのものは失敗させない** ── 休み・連絡が届くことのほうが大事で、
        見えないことは設定画面が別に知らせています。
        """
        if not self.master_path or self.master_path == self.source_path:
            return
        from .. import access_control
        from ..importer import import_master_table

        # **マスタDBが変わっていなければ開かない。** 共有フォルダのマスタは、ほかの人が
        # 新しいものに差し替える。開いているあいだ(Windows)は差し替えられないので、
        # 受信のたび(既定20秒)に開き直さない。見るのは大きさと更新時刻だけ(開かない)
        try:
            info = Path(self.master_path).stat()
            seen = (info.st_size, info.st_mtime_ns)
        except OSError:
            seen = None
        if seen is not None and seen == self._master_seen:
            return
        done = True

        try:
            import_master_table(conn, self.master_path, config.TABLE_MEMBER)
        except (source_db.SourceError, OSError, ValueError,
                sqlite3.Error) as exc:
            done = False
            debug_log(f"AutoSync.receive: 班員名簿を入れ直せませんでした {exc}")

        # **アクセス権限も同じく入れ直す**(この端末が使えるライン)。
        # 表がまだ無ければ作る ── マスタ管理から行を足せるようにするため。
        # 作るのは1度だけ(あると分かったら、次からは見に行かない)
        wrote = not self._access_ready
        try:
            if not self._access_ready:
                with source_db.connect(Path(self.master_path),
                                       read_only=False) as master:
                    access_control.ensure_table(master)
                self._access_ready = True
            import_master_table(conn, self.master_path, access_control.TABLE)
            # 表が変わっていれば、この端末のラインを合わせる
            access_control.enforce(conn)
        except (source_db.SourceError, OSError, ValueError,
                sqlite3.Error) as exc:
            done = False
            debug_log(f"AutoSync.receive: アクセス権限を入れ直せませんでした {exc}")
        if done:
            if wrote:
                # アクセス権限の表を作った(自分で書いた)ので、その分だけ更新時刻が
                # 変わった。書いたあとを覚える(次の受信で開き直さない)
                try:
                    info = Path(self.master_path).stat()
                    seen = (info.st_size, info.st_mtime_ns)
                except OSError:
                    seen = None
            self._master_seen = seen

    # ------------------------------------------------------------------
    def sync_once(self, *, receive: bool = True) -> SyncStatus:
        """送信 → 受信 を 1 回行い、状態を返す。例外は投げない。"""
        status = SyncStatus()
        if not self.enabled:
            status.state = SyncState.DISABLED
            return status

        conn = self._connect()
        try:
            try:
                self.send(conn)
            except TransportError as exc:
                status.state = SyncState.OFFLINE
                status.message = str(exc)
                status.pending = _pending_count(conn)
                debug_log(f"AutoSync.sync_once: 送信できず {exc}")
                return status

            if receive:
                try:
                    self.receive(conn)
                except (source_db.SourceError, OSError, ValueError) as exc:
                    # 送信はできているので、表示が少し古いだけ
                    status.state = SyncState.OFFLINE
                    status.message = f"取り込みに失敗: {exc}"
                    status.pending = _pending_count(conn)
                    debug_log(f"AutoSync.sync_once: 取り込めず {exc}")
                    return status

            status.pending = _pending_count(conn)
            status.state = SyncState.SYNCED if not status.pending else SyncState.PENDING
            status.last_sent_at = db.get_meta(conn, "last_sent_at")
            status.last_received_at = db.get_meta(conn, "last_received_at")
            return status
        finally:
            conn.close()

    def current_status(self, conn: sqlite3.Connection) -> SyncStatus:
        """今の状態だけを組み立てる (通信はしない)。"""
        status = SyncStatus()
        status.pending = _pending_count(conn)
        status.last_sent_at = db.get_meta(conn, "last_sent_at")
        status.last_received_at = db.get_meta(conn, "last_received_at")
        if not self.enabled:
            status.state = SyncState.DISABLED
        elif status.pending:
            status.state = SyncState.PENDING
        else:
            status.state = SyncState.SYNCED
        return status


def _pending_count(conn: sqlite3.Connection) -> int:
    return total_pending_count(conn)


def describe_environment() -> str:
    """同期が使えるかどうかを説明する文字列 (設定画面や起動時の案内用)。

    取り込み元が sqlite3 になってから、**この端末で使えないことは無い**。
    ドライバの有無で動いたり動かなかったりしなくなったので、ここが返すのは
    そのことの説明だけになっている。
    """
    return ("取り込み元は sqlite3 なので、この端末から直接読み書きできます"
            "(Access のドライバは要りません)。")
