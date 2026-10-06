"""端末一覧 ── どのPCがどの設定になっているか

【なぜ要るのか】
ライン設定は**その端末にしか無い**ので、外からは見えません。
「コイルの連絡が来ていない」と言われたとき、調べる手立ては
**その端末まで歩いて行って設定画面を開くこと**だけでした。
台数が増えるほど、原因が設定なのか運用なのかを切り分けられません。

そこで各端末が、自分の設定を取り込み元へ**置いていきます**。
マスタ確認から一覧で見られるので、どこに行かなくても

* どのPCがどのラインになっているか
* どのPCがどのフォルダを見ているか(参照パスの取り違え)
* しばらく起動されていないPCはどれか(最終更新)

が分かります。

【書き込みは増やしません】
共有フォルダ上の SQLite は同時書き込みが弱点なので(設計 §4.2)、
**毎回の同期では書きません**。

* 中身が前と変わったとき
* 記録が古くなったとき(``REPORT_INTERVAL_SEC``、既定6時間)

のどちらかだけ書きます。ふだんは1行読むだけです。

【これは権限ではありません】
誰がその端末を使えるかを決めるものではなく、**いまどうなっているかを
見えるようにするだけ**です。名前を偽ることもできます
(そうする動機のある値ではありません)。
"""

from __future__ import annotations

import datetime as _dt
import getpass
import os
import platform
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

from . import config, settings as user_settings
from .dbkit import source_db
from .logging_utils import get_logger

log = get_logger("terminals")

#: 取り込み元に置く表。**保存用DB(連絡帳)側**に持つ ── どの端末も
#: そこへは必ず書いているので、マスタDBが見えない端末でも記録が残る
TABLE = "端末設定"

#: 行を指す鍵。**「ログインID@PC名」で1行**。
#:
#: PC名だけでは足りません。設定は ``%LOCALAPPDATA%`` に置くので
#: **Windows の利用者ごとに別**で、3交替で同じPCを別のIDで使えば
#: ライン設定も別々に持てます。PC名だけを鍵にすると、その2人が
#: 1行を奪い合い、**後から起動したほうの設定だけが見える**ことになります。
#:
#: 逆に、同じIDで別のPCを使う場合は PC名 が違うので別の行になります。
ROW_KEY = "端末キー"

#: 画面に出す列と、その順。**鍵は出しません**(PC名とログインIDで足りる)
COLUMNS = ("PC名", "ログインID", "ライン", "次のライン", "保存用DBフォルダ",
           "マスタDBフォルダ", "版", "最終更新", "未送信のまま終了", "予約日時", "予約者")

#: 予約の列。**実績(``ライン``)とは別に持ちます。**
#:
#: 一覧の ``ライン`` はその端末が「いまこうなっている」と置いていった事実で、
#: そこを直しても端末の ``settings.json`` は変わらず、次の同期で元に戻ります。
#: 直ったように見えて直っていない、が一番たちが悪い。
#:
#: そこで**別の列に「次はこうしてほしい」を置きます**。端末は次に起動した
#: ときにそれを読んで自分の設定に反映し、予約を消してから実績を報告します。
RESERVE = "次のライン"
RESERVE_AT = "予約日時"
RESERVE_BY = "予約者"

#: 同じ内容でも、これだけ経ったら書き直す(生きている端末だと分かるように)
REPORT_INTERVAL_SEC = 6 * 60 * 60

#: 試験で身元を差し替える。本番では設定しない
ENV_PC_NAME = "CALENDAR_PC_NAME"
ENV_LOGIN_ID = "CALENDAR_LOGIN_ID"

_CREATE = f'''
CREATE TABLE IF NOT EXISTS "{TABLE}" (
    "{ROW_KEY}"        TEXT PRIMARY KEY,
    "PC名"            TEXT NOT NULL DEFAULT '',
    "ログインID"       TEXT NOT NULL DEFAULT '',
    "ライン"           TEXT NOT NULL DEFAULT '',
    "次のライン"        TEXT NOT NULL DEFAULT '',
    "保存用DBフォルダ"  TEXT NOT NULL DEFAULT '',
    "マスタDBフォルダ"  TEXT NOT NULL DEFAULT '',
    "版"              TEXT NOT NULL DEFAULT '',
    "最終更新"         TEXT NOT NULL DEFAULT '',
    "未送信のまま終了"   TEXT NOT NULL DEFAULT '',
    "予約日時"         TEXT NOT NULL DEFAULT '',
    "予約者"           TEXT NOT NULL DEFAULT ''
)'''

#: 中身が変わったかを見る列。``最終更新`` は毎回変わるので**含めない**
_WATCHED = ("ログインID", "ライン", "保存用DBフォルダ", "マスタDBフォルダ", "版",
            "未送信のまま終了")


# ---------------------------------------------------------------------------
# この端末
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class Identity:
    """この端末を指す名前。"""

    pc_name: str
    login_id: str

    def label(self) -> str:
        return f"{self.login_id}@{self.pc_name}" if self.login_id else self.pc_name

    def key(self) -> str:
        """一覧で1行を指す鍵。**ログインIDとPC名の両方**で決まる。"""
        return self.label()


def make_key(pc_name: str, login_id: str = "") -> str:
    """一覧で1行を指す鍵を組み立てる。

    **ログインIDを空にすると「そのPCの誰でも」**という意味になります
    (前もって設定するとき、誰がログインするかは分からないため)。
    """
    pc_name = str(pc_name or "").strip()
    login_id = str(login_id or "").strip()
    return f"{login_id}@{pc_name}" if login_id else pc_name


def identity() -> Identity:
    """PC名 とログインID。**OS が持っている事実**で、画面からは変えられない。"""
    pc_name = os.environ.get(ENV_PC_NAME) or platform.node() or "(不明)"
    login_id = os.environ.get(ENV_LOGIN_ID)
    if not login_id:
        try:
            login_id = getpass.getuser()
        except Exception:                         # noqa: BLE001 - 名前が引けない環境
            login_id = ""
    return Identity(pc_name=str(pc_name).strip(), login_id=str(login_id).strip())


def snapshot() -> dict[str, Any]:
    """いまのこの端末の設定。"""
    from . import app_config

    who = identity()
    return {
        ROW_KEY: who.key(),
        "PC名": who.pc_name,
        "ログインID": who.login_id,
        "ライン": user_settings.get_my_line() or "(未設定)",
        # **いま効いている値**を載せる。保存したものだけだと、配布時の既定で
        # 動いている端末が「未設定」に見える
        "保存用DBフォルダ": config.data_db_dir_text(),
        "マスタDBフォルダ": config.master_db_dir_text(),
        "版": app_config.version_label(),
        "最終更新": _dt.datetime.now().strftime(config.DATETIME_FORMAT),
        "未送信のまま終了": _last_unsent_text(),
    }


def _last_unsent_text() -> str:
    """前回、未送信を残したまま終わったか。

    **書きたい場面では共有に届かない。** 未送信のまま終わるのは共有が
    落ちているときなので、その瞬間に端末一覧へ書くことはできない。
    手元に覚えておき(``sync_service.remember_exit``)、**次につながった
    ときに**ここで載せる ── 後からになるが、「この端末だけ連絡が
    届いていない」を探すには足りる。
    """
    from . import sync_service

    count, when = sync_service.last_unsent_exit()
    if not count:
        return ""
    return f"{count}件 ({when})" if when else f"{count}件"


# ---------------------------------------------------------------------------
# 取り込み元へ置く
# ---------------------------------------------------------------------------
def ensure_table(source: source_db.SourceConnection) -> None:
    """表と列を整える。**既にあるなら1文字も書かない。**

    見てから書く ── 毎回 ``CREATE TABLE IF NOT EXISTS`` を投げると、
    何も変わらないのに共有ファイルへ書き込みの手を伸ばすことになる
    (共有フォルダ上の SQLite では、それが一番避けたいこと)。

    列を足すのは、**前の版が作った表がもう共有に置かれている**ため。
    列が増えた版を入れた端末だけが書けない、を作らない。
    """
    if not source.has_table(TABLE):
        source.execute(_CREATE)
        return

    present = set(source.columns(TABLE))
    if ROW_KEY not in present:
        _rekey(source, present)
        return

    for name in COLUMNS:
        if name in present:
            continue
        source.execute(
            f"ALTER TABLE {source_db.quote_identifier(TABLE)} "
            f"ADD COLUMN {source_db.quote_identifier(name)} TEXT "
            "NOT NULL DEFAULT ''")
        log.info("端末設定に「%s」列を足しました", name)


#: 作り直すときの仮の名前
_REBUILD = f"{TABLE}_作り直し"


def _rekey(source: source_db.SourceConnection, present: set) -> None:
    """**PC名を鍵にしていた古い表を、作り直す。**

    前の版は ``PC名`` を主キーにしていました。3交替で同じPCを別のIDで
    使うと、2人が1行を奪い合い、後から起動したほうの設定だけが見えます
    (設定は ``%LOCALAPPDATA%`` にあるので**利用者ごとに別**)。
    主キーは後から変えられないので、表ごと作り直します。

    **一度きりの作り直しです。** 途中で落ちても失うのは一覧だけで、
    業務データではありません ── 各端末が次につないだときに自分の行を
    置き直すので、放っておいても埋まります。とはいえ共有の業務ファイルを
    触るので、**先に控えを取ります。**
    """
    from . import app_config

    log.info("端末設定の鍵を PC名 から「%s」へ作り直します", ROW_KEY)
    try:
        source_db.backup(source.path, app_config.local_dir("backup"))
    except Exception as exc:                      # noqa: BLE001 - 控えは best effort
        log.warning("作り直しの前に控えを取れませんでした: %s", exc)

    quoted = source_db.quote_identifier(TABLE)
    rebuild = source_db.quote_identifier(_REBUILD)
    # 前回の作り直しが途中で終わっていたら片付ける
    source.execute(f"DROP TABLE IF EXISTS {rebuild}")
    source.execute(_CREATE.replace(f'"{TABLE}"', f'"{_REBUILD}"'))

    # 移せる列だけ移す。**古い表に無い列は既定のまま**
    movable = [name for name in COLUMNS if name in present]
    names = ", ".join(source_db.quote_identifier(n) for n in movable)
    # 鍵は「ログインID@PC名」。ログインIDが空なら PC名 だけ(``Identity.label``)
    key_expr = ("CASE WHEN COALESCE(\"ログインID\", '') = '' THEN \"PC名\" "
                "ELSE \"ログインID\" || '@' || \"PC名\" END"
                if "ログインID" in present else '"PC名"')
    source.execute(
        f"INSERT INTO {rebuild} ({source_db.quote_identifier(ROW_KEY)}, {names}) "
        f"SELECT {key_expr}, {names} FROM {quoted} "
        f'WHERE COALESCE("PC名", \'\') <> \'\' GROUP BY {key_expr}')
    source.execute(f"DROP TABLE {quoted}")
    source.execute(f"ALTER TABLE {rebuild} RENAME TO {quoted}")
    log.info("端末設定を作り直しました")


def report(source: source_db.SourceConnection, *, force: bool = False) -> bool:
    """この端末の設定を取り込み元へ置く。書いたら ``True``。

    **毎回は書かない。** 中身が変わったときと、古くなったときだけ
    (この判断がここに無いと、端末の台数ぶん共有への書き込みが増える)。
    """
    now = snapshot()
    try:
        ensure_table(source)
        rows = source.query(
            f'SELECT * FROM {source_db.quote_identifier(TABLE)} '
            f'WHERE {source_db.quote_identifier(ROW_KEY)} = ?',
            [now[ROW_KEY]])
    except source_db.SourceError as exc:
        # **記録できなくても同期は続ける。** これは見るためのもので、
        # 業務データではない
        log.warning("端末設定を読めませんでした: %s", exc)
        return False

    current = rows[0] if rows else None
    if not force and current is not None and not _needs_update(current, now):
        return False

    try:
        if current is None:
            source.insert(TABLE, now)
        else:
            values = {k: v for k, v in now.items() if k != ROW_KEY}
            source.update(TABLE, values, {ROW_KEY: now[ROW_KEY]})
    except source_db.SourceError as exc:
        log.warning("端末設定を書けませんでした: %s", exc)
        return False
    log.info("端末設定を記録しました: %s / ライン=%s", now[ROW_KEY], now["ライン"])
    return True


# ---------------------------------------------------------------------------
# 前もって決めておいたラインを受け取る
# ---------------------------------------------------------------------------
def take_reservation(source: source_db.SourceConnection) -> str:
    """予約されたラインがあれば自分の設定に入れて、予約を消す。

    戻り値は入れたライン名。**無ければ空文字**。

    【なぜ予約なのか】
    一覧の ``ライン`` を直しても、その端末の ``settings.json`` は変わらず
    次の同期で元に戻ります。**直ったように見えて直っていない**のが
    一番たちが悪いので、直すのではなく「次はこうしてほしい」を別の列に
    置いてもらい、端末が自分で取りに来ます。

    【2通りの予約を見る】
    ``ログインID@PC名`` の行が自分向け、``PC名`` だけの行は
    **そのPCの誰でも**向け。配る前は誰がログインするか分からないので、
    PC名だけで置けるようにしてあります。自分向けが優先です。

    【効くのは次の起動のとき】
    使っている最中に黙って切り替わると、**その人には何も起きていないように
    見えたまま表示だけがずれます**。それは今までの不具合そのものなので、
    起動のときだけ見ます。
    """
    who = identity()
    try:
        if not source.has_table(TABLE):
            return ""
        if RESERVE not in source.columns(TABLE):
            return ""                             # 前の版の表。次の報告で足りる
        rows = source.query(
            f'SELECT * FROM {source_db.quote_identifier(TABLE)} '
            f'WHERE {source_db.quote_identifier(ROW_KEY)} IN (?, ?)',
            [who.key(), who.pc_name])
    except source_db.SourceError as exc:
        log.warning("予約を読めませんでした: %s", exc)
        return ""

    # 自分向け(ログインIDまで一致)が先。無ければPC単位のもの
    by_key = {str(r.get(ROW_KEY, "")): r for r in rows}
    for key in (who.key(), who.pc_name):
        row = by_key.get(key)
        if row is None:
            continue
        line = str(row.get(RESERVE, "") or "").strip()
        if not line:
            continue
        if line not in config.ALL_LINE_NAMES:
            # **一覧に無いものは選べない**(設計 §1)。消して先へ進む ──
            # 残すと毎回同じ警告が出るだけで、誰も直せない
            log.warning("予約されたライン「%s」は一覧にありません。予約を消します", line)
            _clear_reservation(source, key)
            continue
        user_settings.save_my_line(line)
        _clear_reservation(source, key)
        log.info("予約されていたラインを設定しました: %s (予約者: %s)",
                 line, row.get(RESERVE_BY, "") or "不明")
        return line
    return ""


def _clear_reservation(source: source_db.SourceConnection, key: str) -> None:
    """受け取った予約を消す。**二度効かせない。**"""
    try:
        source.update(TABLE, {RESERVE: "", RESERVE_AT: "", RESERVE_BY: ""},
                      {ROW_KEY: key})
    except source_db.SourceError as exc:
        log.warning("予約を消せませんでした: %s", exc)


def apply_reservation() -> str:
    """起動のときに1回だけ。予約があれば自分の設定に入れる。

    **失敗しても黙って進みます** ── 参照パスが未設定・共有が見えないのは、
    起動を止める理由になりません。
    """
    from . import sources

    found = sources.find_data_db()
    if found is None:
        return ""
    try:
        with source_db.connect(found, read_only=False) as source:
            return take_reservation(source)
    except (source_db.SourceError, OSError) as exc:
        log.warning("予約を受け取れませんでした: %s", exc)
        return ""


def _needs_update(current: dict[str, Any], now: dict[str, Any]) -> bool:
    """書き直すか。中身が変わったか、記録が古いか。"""
    for name in _WATCHED:
        if str(current.get(name, "")) != str(now.get(name, "")):
            return True
    return _age_seconds(str(current.get("最終更新", ""))) >= REPORT_INTERVAL_SEC


def _age_seconds(text: str) -> float:
    """記録からの経過秒。読めない値は「とても古い」として扱う。"""
    try:
        stamp = _dt.datetime.strptime(text, config.DATETIME_FORMAT)
    except (ValueError, TypeError):
        return float("inf")
    return (_dt.datetime.now() - stamp).total_seconds()


def publish() -> bool:
    """いまの設定を取り込み元へ**すぐ**置く(ライン設定を変えた直後など)。

    次の同期まで待つと、直したのに一覧が古いままに見えます。

    **失敗しても黙って進みます。** 参照パスが未設定・共有が見えない、は
    ここでは断る理由になりません ── 記録できないことと、設定を変えられた
    ことは別の話です。
    """
    from . import sources

    found = sources.find_data_db()
    if found is None:
        return False
    try:
        with source_db.connect(found, read_only=False) as source:
            return report(source, force=True)
    except (source_db.SourceError, OSError) as exc:
        log.warning("端末設定を記録できませんでした: %s", exc)
        return False


# ---------------------------------------------------------------------------
# 読む
# ---------------------------------------------------------------------------
def listing(path: Optional[Path] = None) -> list[dict[str, Any]]:
    """記録されている端末を全部。読めなければ空。

    並びは **PC名の順**。最終更新の順にすると、見るたびに行が入れ替わって
    「さっき見た行」を目で追えない。
    """
    from . import db, sources

    target = path if path is not None else sources.find_data_db()
    if target is None:
        return []
    try:
        with source_db.connect(target, read_only=True) as source:
            if not source.has_table(TABLE):
                return []
            rows = source.query(
                f"SELECT * FROM {source_db.quote_identifier(TABLE)}")
    except source_db.SourceError as exc:
        log.warning("端末設定を読めませんでした: %s", exc)
        return []

    result = [{name: db.sanitize(row.get(name, ""))
               for name in (ROW_KEY, *COLUMNS)}
              for row in rows]
    # **PC名の順**。最終更新の順にすると、見るたびに行が入れ替わって
    # 「さっき見た行」を目で追えない。同じPCが複数あるならIDの順
    result.sort(key=lambda r: (str(r.get("PC名", "")), str(r.get("ログインID", ""))))
    return result
