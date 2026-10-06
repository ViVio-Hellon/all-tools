"""いま誰が開いているかを、見えるようにする(VBA の ``Form状態管理``)

【なぜ要るのか】
倉庫が確認している最中に現場の画面が開いていれば、**これから看板が出るかも
しれない**と身構えられます。逆に現場からは、倉庫が見ているかどうかが分かり
ます。VBA 版も ``Form状態管理`` に開いた印を付けていましたが、

* 印を付けるのは**開いた瞬間だけ**
* 見るには**自分でダブルクリックして更新**する

という作りだったので、ふたつ困ることがありました。

1. **異常終了すると開いたままになる。** 印を消す経路を通らずに落ちると、
   誰も開いていないのに「開いています」と出続けます。一度でもそうなると、
   その表示は二度と信じられません
2. **押さないと分からない。** 警戒のための表示なのに、警戒しようと思って
   押さないと見えません

ここでは印を付け直し続けます(心拍)。``更新日時`` を毎回書くので、
**印が立っていても、しばらく打たれていなければ「もう居ない」と判る**ように
なります。画面は帯を定期的に取り直しているので、押さなくても変わります。

【時計のずれに寄りかからない】
``更新日時`` は**相手の端末の時計**で書かれます。工場の端末どうしで時刻が
揃っている保証はないので、こちらの時刻と引き算しません。代わりに
「``更新日時`` の値が**変わった**のを見たこちらの時刻」(``seen_at``)を
手元に持ち、それだけで古さを判じます ── 相手が打ち続けるかぎり値は変わり、
``seen_at`` も進みます。

【書くのは直接、読むのは取り込みに相乗り】
心拍は**共有DBへ直接書きます**。未反映の待ち行列(``dirty``)に入れると、
「未反映 N 件」に混ざって本当の書き残しが埋もれ、失敗すれば延々と再試行
されます ── 心拍は**届かなければ諦めてよい**種類の書き込みです。

読むほうは取り込み(:func:`kanban.db.sync.import_all`)が
``Form状態管理`` をすでに読んでいるので、**共有フォルダへの往復は増えません**。
そのぶん新しさは取り込み間隔(``import_interval_sec``)が上限になります。
"""

from __future__ import annotations

import socket
import threading
from dataclasses import asdict, dataclass
from datetime import datetime
from typing import Any, Sequence

from . import applog, config
from .db.shared import SharedDb, _ident

#: 心拍の間隔。
#:
#: **共有ファイルへの書き込みは、思っているより高くつきます。** 書けば
#: 更新時刻が変わり、変われば全端末の「中身が同じなら写さない」
#: (:meth:`kanban.db.shared.SharedDb.local_copy`)が外れます。つまり
#: 心拍 1 回が、**全端末にファイル 1 個ぶんのコピーをやらせます**。
#:
#: 誰も操作していない時間帯(夜間・休憩中・閉じ忘れ)にも効くので、
#: ここは業務の速さではなく**共有フォルダの静けさ**で決めます。
#: 実測: 心拍なしなら取り込み 6 回でコピー 0 回、30 秒心拍では 60 秒に 2 回。
#:
#: 「誰が開いているか」は数分の粒度で足りる情報です ── 秒単位で知っても
#: することは変わりません。
HEARTBEAT_SEC = 120.0

#: これだけ ``更新日時`` が変わらなければ「もう居ない」とみなす。
#:
#: 心拍の間隔だけでは足りません ── 相手が打ってから、こちらが取り込んで
#: 気付くまでに、取り込み間隔ぶんの遅れが乗るためです
#: (120 秒心拍 + 30 秒取り込みで、健全でも 150 秒前後は古く見える)。
#: 生きているものを死んだと言うほうが害が大きいので、余裕を取ります。
#:
#: **正常に閉じたときは即座に消えます**(閉じるときに ``閉`` を書くため)。
#: ここが効くのは異常終了したときだけなので、長くても困りません。
STALE_SEC = 600.0


def host_name() -> str:
    try:
        return socket.gethostname()
    except OSError:  # pragma: no cover - 名前が引けない環境
        return "?"


def registers(mode: str) -> bool:
    """このモードは「開いています」と名乗るか。

    **倉庫参照モードは名乗りません。** 読むだけの端末なので、誰かを
    身構えさせる理由がありません。ここを名乗らせると「倉庫が開いている」が
    常時点灯し、本物の倉庫と見分けが付かなくなります。
    """
    return mode in (config.MODE_SITE, config.MODE_WAREHOUSE)


def registered_line(mode: str, line: str) -> str:
    """この端末が ``Form状態管理`` のどの行を名乗るか(VBA と同じ)。

    **名乗らないモードでは空。** 表示から自分を外すのにも同じ値を使うので、
    ここで ``line`` を返してしまうと、倉庫参照の端末が「設定に書いてある
    ライン」を**自分だと勘違いして隠します** ── 名乗っていないのに、
    その現場が開いていることだけ見えなくなる。
    """
    if not registers(mode):
        return ""
    if mode == config.MODE_WAREHOUSE:
        return config.WAREHOUSE_LINE_NAME
    return line


# ------------------------------------------------------------------
# 見る側
# ------------------------------------------------------------------
@dataclass
class Seen:
    """よその端末 1 つぶんの見え方。"""

    line: str
    label: str
    host: str = ""
    stamp: str = ""
    """相手が書いた ``更新日時``(相手の時計。表示にだけ使う)。"""

    live: bool = False
    """いま開いているか。印が立っていて、かつ最近打たれている。"""

    stale: bool = False
    """印は立っているが、しばらく打たれていない(異常終了の疑い)。"""

    quiet_sec: float = 0.0
    """最後に ``更新日時`` が変わってから、こちらの時計で何秒経ったか。"""


def read(
    store,
    *,
    exclude: str = "",
    now: datetime | None = None,
    stale_sec: float = STALE_SEC,
) -> list[Seen]:
    """いま開いている端末を返す。**手元の写しだけを見ます**(共有へ行かない)。

    ``exclude`` に自分の行を渡すと、自分は落とします ── 自分が開いている
    ことは画面を見れば分かるので、並べても場所を取るだけです。
    """
    at = now or datetime.now()
    out: list[Seen] = []
    for row in store.line_statuses():
        line = str(row.get("line", ""))
        if not line or line == exclude:
            continue
        if str(row.get("remote_status", "")).strip() != config.STATE_OPEN:
            continue

        quiet = _quiet_seconds(str(row.get("seen_at", "")), at)
        stale = quiet is None or quiet > stale_sec
        out.append(
            Seen(
                line=line,
                label=_label(line),
                host=str(row.get("remote_host", "")),
                stamp=str(row.get("remote_stamp", "")),
                live=not stale,
                stale=stale,
                quiet_sec=round(quiet or 0.0, 1),
            )
        )
    return out


def to_dicts(seen: Sequence[Seen]) -> list[dict[str, Any]]:
    return [asdict(s) for s in seen]


def _label(line: str) -> str:
    if line == config.WAREHOUSE_LINE_NAME:
        return config.WAREHOUSE_LINE_NAME
    return config.display_name(line)


def _quiet_seconds(seen_at: str, at: datetime) -> float | None:
    """``seen_at`` から何秒経ったか。読めなければ ``None``(= 古い扱い)。"""
    from .domain.models import parse_datetime

    parsed = parse_datetime(seen_at)
    if parsed is None:
        return None
    return max(0.0, (at - parsed).total_seconds())


# ------------------------------------------------------------------
# 名乗る側
# ------------------------------------------------------------------
class Heartbeat:
    """「開いています」を打ち続ける。**失敗しても黙って次へ。**

    共有フォルダが一時的に落ちている程度のことで、看板の操作を止めては
    いけません ── これは付加情報であって、業務そのものではありません。
    """

    def __init__(
        self,
        gateway: SharedDb | None,
        store,
        line: str,
        *,
        host: str = "",
        interval_sec: float = HEARTBEAT_SEC,
    ) -> None:
        self.gateway = gateway
        """接続先。**差し替えられる** ── 設定画面から接続先を変えたとき、
        心拍だけ古い共有DBへ打ち続けないようにするため。"""

        self.store = store
        self.line = line
        self.host = host or host_name()
        self.interval_sec = interval_sec
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._warned = False

    # -- 1 回ぶん -------------------------------------------------------
    def beat(self, status: str = config.STATE_OPEN) -> bool:
        """1 回打つ。打てたら ``True``。"""
        gateway = self.gateway
        if gateway is None or not self.line:
            return False
        try:
            return self._write(gateway, status)
        except Exception as exc:  # noqa: BLE001 - 付加情報のために落とさない
            self._warn("状態を書けませんでした: %s", exc)
            return False

    def _write(self, gateway: SharedDb, status: str) -> bool:
        columns = self._state_columns()
        now = datetime.now().strftime("%Y/%m/%d %H:%M:%S")

        sets: dict[str, Any] = {config.COL_STATE_VALUE: status}
        if config.COL_STATE_UPDATED_AT in columns:
            sets[config.COL_STATE_UPDATED_AT] = now
        if config.COL_STATE_HOST in columns:
            sets[config.COL_STATE_HOST] = self.host

        assigns = ", ".join(f"{_ident(c)} = ?" for c in sets)
        sql = (
            f"UPDATE {_ident(config.TABLE_STATE)} SET {assigns} "
            f"WHERE {_ident(config.COL_STATE_LINE)} = ?"
        )
        results = gateway.execute([(sql, list(sets.values()) + [self.line])])
        result = results[0]
        if not result.ok:
            self._warn("状態を書けませんでした: %s", result.error_message)
            return False
        if result.affected:
            self._warned = False
            return True

        # まだこのラインの行が無い。**足す番号は手元の写しから決める**
        # (共有DBをもう一度読みに行かない)
        return self._insert(gateway, sets, columns)

    def _insert(
        self, gateway: SharedDb, sets: dict[str, Any], columns: set[str]
    ) -> bool:
        values = {config.COL_STATE_LINE: self.line, **sets}
        if config.COL_KEY in columns:
            values[config.COL_KEY] = self.store.max_status_sort_order() + 1

        names = list(values)
        sql = (
            f"INSERT INTO {_ident(config.TABLE_STATE)} "
            f"({', '.join(_ident(c) for c in names)}) "
            f"SELECT {', '.join('?' for _ in names)} "
            f"WHERE NOT EXISTS (SELECT 1 FROM {_ident(config.TABLE_STATE)} "
            f"WHERE {_ident(config.COL_STATE_LINE)} = ?)"
        )
        results = gateway.execute([(sql, [values[c] for c in names] + [self.line])])
        result = results[0]
        if not result.ok:
            self._warn("状態の行を足せませんでした: %s", result.error_message)
            return False
        if result.affected:
            applog.info("状態管理に %s の行を足しました", self.line)
        self._warned = False
        return True

    def _state_columns(self) -> set[str]:
        """``Form状態管理`` にある列。取り込みが覚えた値を使う。

        まだ一度も取り込めていなければ、**あるものとして書きます** ── 無い
        列を指せば断られるだけで、そのときは ``状態`` だけになります。
        """
        from .db.sync import META_STATE_COLUMNS

        raw = self.store.get_meta(META_STATE_COLUMNS, "")
        if not raw:
            return {
                config.COL_KEY,
                config.COL_STATE_LINE,
                config.COL_STATE_VALUE,
                config.COL_STATE_UPDATED_AT,
                config.COL_STATE_HOST,
            }
        return {c for c in raw.split(",") if c}

    def _warn(self, message: str, *args) -> None:
        """**同じ不調を繰り返し書かない。** 30 秒ごとにログが埋まる。"""
        if self._warned:
            applog.debug(message, *args)
            return
        self._warned = True
        applog.warning(message, *args)

    # -- 回し続ける -----------------------------------------------------
    def start(self) -> None:
        if self._thread is not None:
            return
        self.beat()
        self._thread = threading.Thread(
            target=self._loop, name="presence", daemon=True
        )
        self._thread.start()
        applog.info(
            "状態管理に名乗りました: %s (%s) %.0f秒ごと",
            self.line, self.host, self.interval_sec,
        )

    def _loop(self) -> None:
        while not self._stop.wait(self.interval_sec):
            self.beat()

    def close(self) -> None:
        """閉じたことを 1 回書く。**届かなくても待たない。**

        届かなくても、こちらが打つのをやめれば ``更新日時`` が古くなるので、
        いずれ「もう居ない」と分かります ── 印を消すのは、そこまで待たずに
        伝えるための気配りです。
        """
        self._stop.set()
        thread, self._thread = self._thread, None
        if thread is not None:
            thread.join(timeout=1.0)
        self.beat(config.STATE_CLOSED)


# ------------------------------------------------------------------
# プロセスに 1 つ
# ------------------------------------------------------------------
_current: Heartbeat | None = None


def install(
    gateway: SharedDb | None, store, line: str, **kwargs
) -> Heartbeat | None:
    """心拍を立てる。ラインが決まっていなければ立てない。"""
    global _current
    if _current is not None:
        return _current
    if not line:
        applog.info("ラインが決まっていないので状態管理には名乗りません")
        return None
    _current = Heartbeat(gateway, store, line, **kwargs)
    _current.start()
    return _current


def get() -> Heartbeat | None:
    return _current


def reset() -> None:
    """テスト用。立っていれば止める。"""
    global _current
    if _current is not None:
        _current._stop.set()
        _current._thread = None
    _current = None


def set_gateway(gateway: SharedDb | None) -> None:
    """接続先が変わったので差し替える(設定画面の「接続先」)。"""
    if _current is not None:
        _current.gateway = gateway


def set_line(line: str) -> Heartbeat | None:
    """担当ラインが変わったので、名乗る行を移す(設定画面の「担当ライン」)。

    **古いほうに ``閉`` を書いてから、新しいほうを名乗ります。** これをせず
    にラインだけ変えていたので、心拍は**前のラインを名乗り続け**ました。
    起きていたこと:

    * 前のラインが共有DBで ``開`` のまま残り、他の端末には**居もしない
      端末が開いて見える**(``STALE_SEC`` の 10 分が経つと ``L-1?`` のように
      破線の ``?`` になる)
    * 新しいラインは一度も名乗らないので、本当に開いているほうが見えない
    * 自分の画面の「開いている端末」からも自分を外せなくなる ── 除外は
      **いまの**ラインで引くのに、印は**前の**ラインに立っているため、
      **自分自身が他人として並びます**

    ラインを切り替えながら試すと必ず踏みます。
    """
    global _current
    if _current is None:
        return None
    if _current.line == line:
        return _current

    old = _current
    old.close()  # 前のラインへ「閉」を書いて、打つのをやめる
    applog.info("担当ラインが変わったので名乗り直します: %s → %s", old.line, line or "(なし)")
    if not line:
        _current = None
        return None

    _current = Heartbeat(
        old.gateway, old.store, line, host=old.host, interval_sec=old.interval_sec
    )
    _current.start()
    return _current
