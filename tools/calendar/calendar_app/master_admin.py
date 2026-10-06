"""マスタ管理 ── 直す先は取り込み元だけ

【なぜ要るのか】
班員名簿は起動のたびに黙って読まれるだけで、中を見る手立ても直す手立ても
画面にありませんでした。名前や班が違っていても、現場には
**「休みにしたい人が一覧に出ない」**としか見えません。
Access を開ける人を探すところから始まっていました。

【書き先は取り込み元ただ1つ】
取り込みは**総入れ替え**なので、手元の SQLite を直しても次の取り込みで
消えます(設計 §1 の規則5「同じ事実を2か所に持たない」)。

    画面 → 取り込み元(sqlite3)へ書く → その表だけ取り込み直す → 手元が追いつく

最後の一手を飛ばすと、元は直っているのに画面の動きが変わりません。
**直したのに効かない**が一番たちが悪いので、その場で取り込み直します。

【パスが無ければ直せない】
書き先が決まっていないのに「保存しました」と言うわけにはいきません。
参照パスが未設定・ファイルが見つからない・開けない、のいずれも
``REFUSE_NO_SOURCE`` で断り、**次に何をすればよいか**を返します。

【行の指し方】
``管理番号`` を鍵にします。班員名簿の主キーで、取り込みは
``INSERT OR REPLACE`` でこの値を軸に入れ直すため、**取り込みをまたいで
同じ行を指し続けます**(梱包資材ツールが ``rowid`` を使っているのは、
あちらの管理番号が取り込みのたびに振り直されるためで、事情が違います)。
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable, Optional

from . import access_control, config, sources, terminals
from .logging_utils import get_logger

log = get_logger("master_admin")

#: 一度に出す行数。切ったぶんは必ず数で言う
ROW_LIMIT = 200

#: 行を指す鍵の既定(班員名簿)。**取り込みをまたいで同じ行を指す**
#: (上の説明を参照)。表ごとの鍵は ``row_key()`` で引く
ROW_KEY = "管理番号"


#: 表がどちらの取り込み元にあるか
WHERE_MASTER = "master"     # マスタDB (梱包資材マスタ)
WHERE_DATA = "data"         # 保存用DB (連絡帳)


@dataclass(frozen=True)
class Managed:
    """マスタ確認で扱う表1つ。"""

    table: str
    label: str
    #: 画面に出す列の順。取り込みが読む順(VBA の列位置)と同じ
    columns: tuple[str, ...]
    #: 空にできない列
    required: tuple[str, ...] = ()
    #: どちらの取り込み元にあるか
    where: str = WHERE_MASTER
    #: 直せるか。**見るだけの表もここに並べる** ── 確かめたいものと
    #: 直したいものを別の画面に分けると、探すところから始まる
    editable: bool = True
    #: 行を指す鍵。班員名簿は ``管理番号``、端末一覧は ``端末キー``
    row_key: str = ""
    #: 見るだけの表に添える説明。「なぜ直せないのか」を空欄にしない
    note: str = ""
    #: 直せなくても**消せる**か。端末一覧は、入れ替えたPCの行が
    #: いつまでも残ると「まだ古い設定の端末がある」ように見える
    removable: bool = False
    #: 並べる列。空なら ``row_key``。**新しいものから見たい表**がある
    order: str = ""
    #: 新しい順に並べるか(履歴は、直近を探しに来るのがふつう)
    newest_first: bool = False
    #: **あとから直せる列**。空なら「全部」(班員名簿はこちら)。
    #: 端末一覧は実績が並ぶ表なので、直せるのは予約の列だけ
    editable_columns: tuple[str, ...] = ()
    #: **足すときに打てる列**。空なら「全部」
    create_columns: tuple[str, ...] = ()
    #: 鍵を組み立てる(打ってもらわない鍵がある。端末一覧の ``端末キー``)
    key_builder: Optional[Callable[[dict], str]] = None
    #: 書く前の下ごしらえ(表がまだ無いことがある)
    prepare: Optional[Callable[[Any], None]] = None
    #: 直すのに管理者パスワードが要るか。``admin_password.PROTECTED_LABELS`` の鍵
    guard_as: str = ""
    #: その表が取り込み元に無いと直せないか。端末一覧は**まだ無くてよい**
    #: (1台も起動していない共有に、前もって登録できるようにする)
    requires_table: bool = True
    #: 列ごとの添え書き(列名 → 文言)
    placeholders: tuple[tuple[str, str], ...] = ()
    #: 列ごとの入力候補(列名 → 候補)。**候補以外も打てる**(共用の表で、
    #: 別ツールの値も入るため)
    suggestions: tuple[tuple[str, tuple[str, ...]], ...] = ()
    #: 鍵を**取り込み元が振る**か(``INTEGER PRIMARY KEY``)。打ってもらわない
    auto_key: bool = False
    #: **どの列を変えても**管理者パスワードが要るか(アクセス権限)。
    #: 偽なら予約の列を触るときだけ(端末一覧)
    guard_all: bool = False
    #: 書く前に値を確かめる(駄目なら断る文言を返す)
    validate: Optional[Callable[[dict, bool], str]] = None


#: マスタ確認で扱う表。
#: 休み管理・削除履歴は**業務データ**であって、直すのはカレンダー画面から。
MANAGED: tuple[Managed, ...] = (
    Managed(
        table=config.TABLE_MEMBER,
        label="班員名簿",
        columns=("管理番号", "苗字", "班", "名前", "読み", "担当ライン"),
        required=("管理番号", "名前"),
        where=WHERE_MASTER,
        row_key="管理番号",
    ),
    Managed(
        table=terminals.TABLE,
        label="端末一覧",
        columns=terminals.COLUMNS,
        where=WHERE_DATA,
        removable=True,
        row_key=terminals.ROW_KEY,
        # **直せるのは予約の列だけ。** ほかは各端末が置いていった事実で、
        # そこを直しても端末の settings.json は変わらず、次の同期で戻る
        editable_columns=(terminals.RESERVE,),
        # 前もって登録するときに打つもの。**鍵は組み立てる**
        create_columns=("PC名", "ログインID", terminals.RESERVE),
        required=("PC名",),
        key_builder=lambda values: terminals.make_key(
            values.get("PC名", ""), values.get("ログインID", "")),
        prepare=terminals.ensure_table,
        requires_table=False,
        guard_as="terminal_line",
        placeholders=((terminals.RESERVE,
                       "次の起動から使うライン(空なら変えない)"),),
        # **画面にそのまま出る文言なので、飾りを入れない。**
        # 強調の記号は `textContent` では記号のまま出る
        note=("どのPCがどのラインになっているかの一覧です。"
              "「ライン」は各端末が置いていった実績なので直せません "
              "(直すとその端末の設定は変わらないまま、次の同期で戻ります)。"
              "前もって決めておきたいときは「次のライン」に入れてください "
              "── その端末が次に起動したときに、自分で受け取ります。"
              "まだ一度も起動していないPCは「行を追加」で登録できます。"
              "入れ替えて使わなくなった端末の行は、消せます。"),
    ),
    Managed(
        table=access_control.TABLE,
        label="アクセス権限",
        columns=access_control.COLUMNS,
        required=("権限",),
        where=WHERE_MASTER,
        row_key="管理番号",
        # 管理番号は取り込み元が振る(打ってもらわない)
        auto_key=True,
        editable_columns=("ログインID", "PC名", "権限", "有効", "備考"),
        create_columns=("ログインID", "PC名", "権限", "有効", "備考"),
        prepare=access_control.ensure_table,
        requires_table=False,
        # **この端末で使えるラインを決める表。** どの列を変えても関門を通す
        guard_as="access",
        guard_all=True,
        validate=lambda values, creating: _check_access(values, creating),
        placeholders=(
            ("ログインID", "Windows のログイン名(空ならどの人でも)"),
            ("PC名", "PC名(空ならどのPCでも)"),
            ("権限", "ライン名(例: コイル)。他ツールの値も入ります"),
            ("有効", "1=有効 / 0=無効(空なら1)"),
        ),
        suggestions=(("権限", (*access_control.line_codes(),
                              *access_control.OTHER_TOOL_CODES)),
                     ("有効", ("1", "0"))),
        note=("この端末で使えるラインを、ログインID と PC名 で決める表です"
              "(梱包資材マスタの表で、梱包資材総合ツールと共用)。"
              "このツールが読むのは「権限」がライン名の行だけで、"
              "mode:field(現場モード)・mode:material(資材モード)などの"
              "他ツールの行はそのまま残します。"
              "空欄は「問わない」ですが、ログインID と PC名 の両方が空の行は"
              "全員への許可になるので効きません。"
              "行を足す・直す・消すには管理者パスワードが要ります。"),
    ),
    Managed(
        table=config.TABLE_DEL_HISTORY,
        label="削除履歴",
        # 消した中身が先。**いつ消したか**より**何が消えたか**を先に読む
        columns=("削除日時", "対象日付", "区分", "登録内容", "識別コード",
                 "班", "ライン", "削除実行者"),
        where=WHERE_DATA,
        editable=False,
        row_key="削除日時",
        order="削除日時",
        newest_first=True,
        note=("カレンダーから削除したものの記録です。残すためのものなので、"
              "ここからは直せません。新しい順に並びます。"
              "上の欄で日付・名前・ラインなどから探せます。"),
    ),
)

BY_TABLE = {m.table: m for m in MANAGED}

#: 直せる表だけ。**見るだけの表をここに入れない**
EDITABLE = {m.table: m for m in MANAGED if m.editable}


def row_key(table: str) -> str:
    """その表で行を指す鍵の列名。"""
    return _key_column(table)


def _key_column(table: str) -> str:
    """``row_key`` と同じ。**引数名に隠されている場所から呼ぶ用。**"""
    managed = BY_TABLE.get(table)
    return (managed.row_key or ROW_KEY) if managed else ROW_KEY


def is_removable(table: str) -> bool:
    """直せなくても、行を消せる表か(端末一覧)。"""
    managed = BY_TABLE.get(table)
    return bool(managed and (managed.editable or managed.removable))


# ---------------------------------------------------------------------------
# 断りの種別。**文言ではなくこれで見分ける**(設計 §1 の規則4)
# ---------------------------------------------------------------------------
REFUSE_NO_SOURCE = "no_source"          # 取り込み元に届かない
REFUSE_NOT_EDITABLE = "not_editable"    # この表は直す表ではない
REFUSE_BAD_VALUE = "bad_value"          # 入れた値の形が違う
REFUSE_NO_ROW = "no_row"                # その行がもう無い
REFUSE_ALREADY = "already"              # もうある
REFUSE_WRITE_FAILED = "write_failed"    # 書けなかった
REFUSE_STALE = "stale_row"              # 開いたあとに誰かが直した


@dataclass
class Result:
    """直せたか。駄目なら**理由と種別**。"""

    ok: bool
    message: str = ""
    reason: str = ""


# ---------------------------------------------------------------------------
# 直せるか (パスが無ければ直せない)
# ---------------------------------------------------------------------------
@dataclass
class Readiness:
    """書ける状態か。**駄目なら、次に何をすればよいかまで持つ。**"""

    ok: bool = False
    path: Optional[Path] = None
    reason: str = ""
    why: str = ""
    #: 読むだけならできるか(パスはあるが書く手段が無い、など)
    can_read: bool = False

    def as_result(self) -> Result:
        return Result(False, self.why, self.reason)


def readiness(table: str = "") -> Readiness:
    """マスタを直せる状態か調べる。

    順番に意味がある ── **表 → 参照パス → 開けるか → 書けるか**。
    届かないことを先に言うと、そもそも直せない表を指定した人に
    「共有が落ちている」と読ませてしまう。
    """
    if table and table not in BY_TABLE:
        return Readiness(reason=REFUSE_NOT_EDITABLE,
                         why=f"{table} はこのツールが扱う表ではありません。")

    managed = BY_TABLE.get(table or config.TABLE_MEMBER)
    where = managed.where if managed else WHERE_MASTER

    if where == WHERE_DATA:
        found = sources.find_data_db()
        label = f"保存用DB({config.SOURCE_FILE_DATA})"
    else:
        found = sources.find_master_db()
        label = f"マスタDB({config.SOURCE_FILE_MASTER})"

    if found is None:
        # **どこを見たのかを言う。** マスタのフォルダが未設定なら保存用と
        # 同じ場所も見ている(``sources.find_master_db``)ので、片方だけ
        # 出すと「設定したのに見つからない」と受け取られる
        return Readiness(
            reason=REFUSE_NO_SOURCE,
            why=(f"{label}が見つかりません。\n"
                 "設定画面の「参照パス」でフォルダを指定してください。\n"
                 f"探した場所: {_searched()}"))

    result = sources.probe(found)
    if not result.ok:
        return Readiness(
            path=found, reason=REFUSE_NO_SOURCE,
            why=f"{label} を開けません: {result.error}\n{found}")

    if managed is not None and not managed.editable:
        # **見るだけの表。** 中身は読めるので ``can_read`` は立てる ──
        # 直せないことと、確かめられないことは別の話
        return Readiness(path=found, can_read=True,
                         reason=REFUSE_NOT_EDITABLE, why=managed.note)

    # **その表が無ければ直せない。** ただし「まだ無くてよい」表もある ──
    # 端末一覧は1台も起動していない共有にも前もって登録できるようにする
    target = managed.table if managed is not None else config.TABLE_MEMBER
    wants_table = managed.requires_table if managed is not None else True
    if wants_table and target not in result.tables:
        return Readiness(
            path=found, reason=REFUSE_NO_SOURCE,
            why=(f"{found.name} に「{target}」表がありません。\n"
                 "班員名簿が入っている取り込み元のフォルダを"
                 "「参照パス」に指定してください。"))

    # ここまで来れば直せる。**取り込み元が sqlite3 になってから、
    # 「この端末からは書けない」という状態は無くなった** ── 以前は
    # Access のドライバ(ACE)が要り、無い端末では見るだけだった
    return Readiness(ok=True, path=found, can_read=True)


def _searched() -> str:
    """マスタDB を探した場所を、実際に見た順で並べる。

    ``sources.find_master_db`` はマスタのフォルダが未設定なら保存用と
    同じ場所も見る。ここでその順序を写す ── 片方だけ出すと
    「設定したのに見つからない」と受け取られる。
    """
    places: list[str] = []
    for directory in (config.master_db_dir(), config.data_db_dir()):
        text = str(directory)
        if text and text != "." and text not in places:
            places.append(text)
    return " / ".join(places) if places else "(参照パスが1つも設定されていません)"


# ---------------------------------------------------------------------------
# 列
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class Column:
    """直せる列1つ。"""

    name: str
    required: bool = False
    #: 鍵の列。作るときだけ入れられ、あとから変えられない
    is_key: bool = False
    #: あとから直せるか。**実績の列は直せない**(端末一覧)
    editable: bool = True
    #: 足すときに打てるか
    at_create: bool = True
    #: 空欄のときに出す添え書き。**空が普通の欄**は、これが無いと
    #: 何を入れるところか分からない
    placeholder: str = ""
    #: 入力候補(候補以外も打てる)
    suggestions: tuple[str, ...] = ()


def columns(table: str, present: Optional[Iterable[str]] = None) -> list[Column]:
    """打ち込める列。

    **3つとも既にある事実を読むだけ**:
    こちらが持つ定義 (``MANAGED``) ∩ 取り込み元に実在する列。
    取り込み元に無い列を出すと、押しても黙って捨てられる。
    """
    managed = BY_TABLE.get(table)
    if managed is None:
        return []
    key = row_key(table)
    actual = {name.strip() for name in present} if present is not None else None
    result = []
    for name in managed.columns:
        if actual is not None and name not in actual:
            continue
        result.append(Column(
            name=name,
            required=name in managed.required,
            is_key=name == key,
            editable=(not managed.editable_columns
                      or name in managed.editable_columns),
            at_create=((not managed.create_columns
                        or name in managed.create_columns)
                       and not (managed.auto_key and name == key)),
            placeholder=dict(managed.placeholders).get(name, ""),
            suggestions=dict(managed.suggestions).get(name, ())))
    return result


def source_columns(path: Path, table: str) -> list[str]:
    """取り込み元に実在する列名。"""
    from .dbkit import source_db

    try:
        with source_db.connect(path, read_only=True) as source:
            return source.columns(table)
    except source_db.SourceError as exc:
        log.warning("%s の列を読めませんでした: %s", table, exc)
        return []


# ---------------------------------------------------------------------------
# 一覧
# ---------------------------------------------------------------------------
@dataclass
class Page:
    """マスタ管理の面ぜんぶ。"""

    table: str = config.TABLE_MEMBER
    label: str = "班員名簿"
    editable: bool = False
    reason: str = ""
    why: str = ""
    source: str = ""
    columns: list[Column] = field(default_factory=list)
    rows: list[dict[str, Any]] = field(default_factory=list)
    total: int = 0
    truncated: bool = False
    query: str = ""
    #: 並べ替えている列(空なら表の既定の並び)と、その向き
    sort: str = ""
    desc: bool = False


def page(table: str = config.TABLE_MEMBER, *, query: str = "",
         sort: str = "", desc: bool = False) -> Page:
    """取り込み元の中身を読んで、直せるかどうかと一緒に返す。

    **読むのは取り込み元**(手元の写しではない)。直す相手を見せないと、
    直した結果が合っているかを確かめられない。
    """
    managed = BY_TABLE.get(table)
    view = Page(table=table, label=managed.label if managed else table,
                query=query)

    ready = readiness(table)
    view.editable = ready.ok
    view.reason = ready.reason
    # **表の説明は、直せる表でも出す。** 何ができて何ができないのかは
    # 断られたときだけ知りたいことではない(端末一覧は、実績と予約が
    # 同じ表に並ぶので、説明が無いとどちらを触ればよいか分からない)
    view.why = ready.why or (managed.note if managed else "")
    if ready.path is None:
        return view
    view.source = str(ready.path)

    present = source_columns(ready.path, table)
    if not present and managed is not None and not managed.requires_table:
        # **まだ1台も記録していない。** 表そのものが無いのは異常ではない
        # ので、列だけ定義どおりに出して「空」と言う
        view.columns = columns(table)
        view.why = (f"{view.label}はまだ1件もありません。"
                    "各端末が同期したときに記録されます。\n"
                    + (managed.note or ""))
        return view
    view.columns = columns(table, present)
    if not view.columns:
        view.editable = False
        view.reason = view.reason or REFUSE_NO_SOURCE
        view.why = view.why or (
            f"{view.label}に、こちらが扱える列が1つもありません。"
            f"(取り込み元の列: {', '.join(present) or 'なし'})")
        return view

    rows = _read_rows(ready.path, table, [c.name for c in view.columns])
    if query:
        needle = query.casefold()
        rows = [r for r in rows
                if any(needle in str(v).casefold() for v in r.values())]
    # **並べ替えは切る前に。** 画面に出すのは ROW_LIMIT 行までなので、
    # 画面の中だけで並べると、出ていない行を含めた正しい順にならない
    if sort and sort in {c.name for c in view.columns}:
        rows = sort_rows(rows, sort, desc)
        view.sort, view.desc = sort, desc
    view.total = len(rows)
    view.truncated = len(rows) > ROW_LIMIT
    view.rows = rows[:ROW_LIMIT]
    return view


def sort_rows(rows: list[dict[str, Any]], column: str,
              desc: bool = False) -> list[dict[str, Any]]:
    """列で並べ替える。

    * **数は数として**並べる(``管理番号`` が 1, 10, 2 の順にならない)
    * 文字は大文字小文字を区別せずに並べる
    * **空欄はどちらの向きでも最後**(空ばかりが先頭に来て、
      中身のある行を探しにいくことにならないように)
    * 同じ値どうしは元の並びを保つ
    """
    def key(row: dict[str, Any]):
        text = str(row.get(column, "") or "").strip()
        try:
            return (0, float(text.replace(",", "")), "")
        except ValueError:
            return (1, 0.0, text.casefold())

    filled = [r for r in rows if str(r.get(column, "") or "").strip()]
    blank = [r for r in rows if not str(r.get(column, "") or "").strip()]
    return sorted(filled, key=key, reverse=desc) + blank


def _read_rows(path: Path, table: str, names: list[str]) -> list[dict[str, Any]]:
    from . import db
    from .dbkit import source_db

    try:
        with source_db.connect(path, read_only=True) as source:
            rows = source.query(
                f"SELECT * FROM {source_db.quote_identifier(table)}")
    except source_db.SourceError as exc:
        log.warning("%s を読めませんでした: %s", table, exc)
        return []

    managed = BY_TABLE.get(table)
    key = row_key(table)
    # **鍵も一緒に持って返す。** 画面に列として出さない表(端末一覧)でも、
    # 消すときに行を指す必要がある
    wanted = list(dict.fromkeys([*names, key]))

    result = []
    for row in rows:
        item = {name: db.sanitize(row.get(name, "")) for name in wanted}
        # 鍵が空の行は**指せない**ので、直したり消したりする表では捨てる
        # (取り込みも同じ理由で捨てている)。見るだけの表では捨てない ──
        # 見せないほうが困る
        if not item.get(key) and (managed is None or managed.editable
                                  or managed.removable):
            continue
        result.append(item)

    order = (managed.order if managed and managed.order else key)
    newest_first = bool(managed and managed.newest_first)
    result.sort(key=lambda r: str(r.get(order, "")), reverse=newest_first)
    return result


# ---------------------------------------------------------------------------
# 直す
# ---------------------------------------------------------------------------
def save_row(conn: sqlite3.Connection, table: str, row_key: str,
             values: dict[str, Any],
             expected: Optional[dict[str, Any]] = None,
             password: str = "") -> Result:
    """1行を直す。書き先は**取り込み元**。

    【``expected`` = 開いたときの中身】
    画面は鍵以外の**全列**を送ります(``views/master.js``)。ですから
    2人が同じ行を開くと、**後から保存したほうが、自分が触っていない欄まで
    古い写しで上書きします。** 先に直した人の修正は黙って消え、
    両方に「直しました」と出ます。

    そこで「開いたときはこうだった」を一緒に受け取り、**そのままなら**
    書きます。変わっていれば書かずに断ります(``REFUSE_STALE``)──
    どちらの修正も消さないためです。

    ``expected`` を渡さない呼び出し(コマンド経由・古い画面)はこれまで
    どおり素通しします。**確かめようが無いものを、断る理由にはしない。**
    """
    managed = BY_TABLE.get(table)
    ready = readiness(table)
    if not ready.ok:
        return ready.as_result()

    key = str(row_key or "").strip()
    if not key:
        return Result(False, "直す行が指定されていません。", REFUSE_BAD_VALUE)

    present = source_columns(ready.path, table)
    cleaned, error = _clean(table, values, present, creating=False)
    if error is not None:
        return error
    if not cleaned:
        return Result(False, "変更する値がありません。", REFUSE_BAD_VALUE)

    blocked = _guard(table, cleaned, password)
    if blocked is not None:
        return blocked

    from . import db
    from .dbkit import source_db

    # 開いたときの値。**送られた列のうち、実在する列だけ**を見る
    guard: dict[str, Any] = {}
    if isinstance(expected, dict):
        guard = {name: db.sanitize(value) for name, value in expected.items()
                 if name in present and name != ROW_KEY}

    try:
        with _connect(ready.path) as source:
            if managed is not None and managed.prepare is not None:
                managed.prepare(source)
            if not _count(source, table, key):
                return Result(False,
                              f"{_key_column(table)} {key} の行は"
                              "取り込み元にもうありません。"
                              "画面を更新してからやり直してください。",
                              REFUSE_NO_ROW)
            # **開いたときのままなら書く。** 1文で確かめて書くので、
            # 確かめてから書くまでの隙間が無い
            changed = source.update(table, _stamped(table, cleaned),
                                    {_key_column(table): key, **guard})
            if guard and not changed:
                return Result(
                    False,
                    f"{ROW_KEY} {key} は、この画面を開いたあとに"
                    "別の端末で直されています。\n"
                    "上書きすると相手の修正が消えるので、いったん止めました。"
                    "画面を更新して、いまの内容を見てからやり直してください。",
                    REFUSE_STALE)
    except source_db.SourceError as exc:
        return _write_failed(exc)

    return _follow(conn, ready.path, table, f"{key} を直しました")


def add_row(conn: sqlite3.Connection, table: str,
            values: dict[str, Any], password: str = "") -> Result:
    """1行を足す。書き先は**取り込み元**。

    **鍵を打ってもらわない表があります**(端末一覧)。``端末キー`` は
    「ログインID@PC名」で組み立てるものなので、打たせると打ち間違いが
    そのまま「誰にも当たらない予約」になります(``key_builder``)。
    """
    managed = BY_TABLE.get(table)
    ready = readiness(table)
    if not ready.ok:
        return ready.as_result()

    present = source_columns(ready.path, table)
    if not present and managed is not None and not managed.requires_table:
        # **まだ表が無い。** 1台も起動していない共有へ前もって登録する道を
        # 塞がないよう、定義どおりの列があるものとして進む(``prepare`` が
        # 書く直前に作る)
        present = [*managed.columns, _key_column(table)]
    cleaned, error = _clean(table, values, present, creating=True)
    if error is not None:
        return error

    key_column = _key_column(table)
    auto = bool(managed is not None and managed.auto_key)
    if auto:
        # **鍵は取り込み元が振る**(``INTEGER PRIMARY KEY``)。打たせない
        cleaned.pop(key_column, None)
        key = ""
    elif managed is not None and managed.key_builder is not None:
        key = str(managed.key_builder(cleaned) or "").strip()
        if key:
            cleaned[key_column] = key
    else:
        key = str(cleaned.get(key_column, "")).strip()
    if not key and not auto:
        return Result(False, f"{key_column} を入力してください。",
                      REFUSE_BAD_VALUE)

    blocked = _guard(table, cleaned, password)
    if blocked is not None:
        return blocked

    from .dbkit import source_db

    try:
        with _connect(ready.path) as source:
            if managed is not None and managed.prepare is not None:
                # 1台も起動していない共有には、まだ表が無い
                managed.prepare(source)
            if not auto and _count(source, table, key):
                return Result(False,
                              f"{key_column} {key} はすでに登録されています。",
                              REFUSE_ALREADY)
            source.insert(table, _stamped(table, cleaned))
    except source_db.SourceError as exc:
        return _write_failed(exc)

    what = key or f"{managed.label if managed else table}に1行"
    return _follow(conn, ready.path, table, f"{what}を追加しました")


def delete_row(conn: sqlite3.Connection, table: str, row_key: str,
               password: str = "") -> Result:
    """1行を消す。書き先は**取り込み元**。

    **直せない表でも「消せる」ことはあります**(``Managed.removable``)。
    端末一覧がそれで、値はその端末しか書けませんが、入れ替えて使わなく
    なったPCの行は誰かが片付けないと残り続けます ── 残っていると
    「まだ古い設定の端末がある」ように見えて、探しに行くことになります。
    """
    managed = BY_TABLE.get(table)
    ready = readiness(table)
    # 直せる表か、消せる表で**取り込み元に届いている**か
    if not ready.ok and not (managed and managed.removable and ready.can_read):
        return ready.as_result()

    key_column = _key_column(table)
    key = str(row_key or "").strip()
    if not key:
        return Result(False, "消す行が指定されていません。", REFUSE_BAD_VALUE)

    # **消すのも同じ関門。** アクセス権限の行を消すと、その端末は
    # 使えるラインを失う(書き足すのと同じ重さ)
    blocked = _guard(table, {}, password, deleting=True)
    if blocked is not None:
        return blocked

    from .dbkit import source_db

    try:
        with _connect(ready.path) as source:
            if not _count(source, table, key):
                return Result(False,
                              f"{key_column} {key} の行は取り込み元にもうありません。",
                              REFUSE_NO_ROW)
            # **WHERE の無い DELETE は source_db が断る。** 空の WHERE は
            # 「全行」で、取り込み元は共有のマスタなので1回の取り違えで
            # 全員が困る
            source.delete(table, {key_column: key})
    except source_db.SourceError as exc:
        return _write_failed(exc)

    return _follow(conn, ready.path, table, f"{key} を削除しました")


# ---------------------------------------------------------------------------
# 後始末
# ---------------------------------------------------------------------------
def _connect(path: Path):
    """書き込みで開く。**控えを取ってから開く。**

    共有フォルダの上の SQLite は Access と違ってロックが OS 任せなので、
    最悪の壊れ方が「破損」になりうる。マスタは全員が使うものなので、
    書く前に戻せる状態を作っておく。

    **1行直すたびに取り直しはしない。** 控えは共有のファイルを丸ごと
    読むので、連続で直すと共有への行き来がそのぶん増える。
    ``source_db.backup`` が間隔を見て、近いうちに取ってあれば省く。
    """
    from . import app_config
    from .dbkit import source_db

    try:
        source_db.backup(path, app_config.local_dir("backup"))
    except Exception as exc:                      # noqa: BLE001 - 控えは best effort
        log.warning("控えを取れませんでした: %s", exc)
    return source_db.connect(path, read_only=False)


def _count(source, table: str, key: str) -> int:
    """その鍵の行が取り込み元にあるか。**鍵の列名は表ごとに違う。**"""
    from .dbkit import source_db

    rows = source.query(
        f"SELECT COUNT(*) AS n FROM {source_db.quote_identifier(table)} "
        f"WHERE {source_db.quote_identifier(_key_column(table))} = ?", [key])
    if not rows:
        return 0
    raw = next(iter(rows[0].values()), 0)
    try:
        return int(str(raw).strip() or 0)
    except ValueError:
        return 0


def _follow(conn: sqlite3.Connection, path: Path, table: str,
            message: str) -> Result:
    """**その表だけ取り込み直して、手元を追いつかせる。**

    ここを飛ばすと、取り込み元は直っているのに画面の動きが変わらない。
    「直したのに効かない」が一番たちが悪い。

    **手元に写しを持たない表は、そもそも追いつかせる必要がありません**
    (端末一覧は画面が取り込み元を直に読む)。取り込み直そうとすると
    「取り込みに対応していない表です」と出て、消えているのに失敗したように
    見えます。
    """
    from .importer import import_master_table, is_importable

    if not is_importable(table):
        log.info("%s", message)
        return Result(True, message)

    try:
        count = import_master_table(conn, path, table)
    except Exception as exc:                      # noqa: BLE001 - 書けてはいる
        log.warning("取り込み直しに失敗しました: %s", exc)
        return Result(True,
                      f"{message}。ただし手元への取り込み直しに失敗しました "
                      f"({exc})。設定画面から取り込み直してください。")
    log.info("%s / 取り込み直し %s 件", message, count)
    return Result(True, f"{message}(取り込み直し {count} 件)")


def _stamped(table: str, values: dict[str, Any]) -> dict[str, Any]:
    """予約を書くときは、**いつ・誰が**も一緒に残す。

    他の端末の設定を決める操作なので、あとから「誰がこれにしたのか」を
    辿れないと、違っていたときに直す相手が分かりません。
    予約を空にするとき(取り消し)は、印も一緒に消します。
    """
    if table != terminals.TABLE or terminals.RESERVE not in values:
        return values

    from . import db

    stamped = dict(values)
    if str(values.get(terminals.RESERVE, "")).strip():
        stamped[terminals.RESERVE_AT] = db.now_string()
        stamped[terminals.RESERVE_BY] = terminals.identity().label()
    else:
        stamped[terminals.RESERVE_AT] = ""
        stamped[terminals.RESERVE_BY] = ""
    return stamped


def _guard(table: str, values: dict[str, Any], password: str, *,
           deleting: bool = False):
    """管理者パスワードが要る変更か。要るなら確かめる。

    **他の端末のラインを決めるのは、その端末でラインを変えるのと同じこと**
    です(むしろ、目の前に無い端末を変えるぶん危ない)。同じ関門を通します。

    端末一覧は予約の列を触るときだけ。アクセス権限は**どの列でも・
    消すときも**(``guard_all``)── どの行も「どの端末がどのラインを
    使えるか」そのものなので。
    """
    managed = BY_TABLE.get(table)
    if managed is None or not managed.guard_as:
        return None
    if managed.guard_all:
        pass                                      # 何を触っても関門
    elif deleting or terminals.RESERVE not in values:
        return None                               # 予約を触らないなら関係ない

    from . import admin_password

    result = admin_password.guard([managed.guard_as], password)
    if result is None:
        return None
    return Result(False, result.message, result.reason)


def _write_failed(exc: Exception) -> Result:
    log.warning("取り込み元へ書けませんでした: %s", exc)
    return Result(False, f"取り込み元へ書けませんでした: {exc}",
                  REFUSE_WRITE_FAILED)


def _clean(table: str, values: dict[str, Any], present: list[str], *,
           creating: bool) -> tuple[dict[str, Any], Optional[Result]]:
    """入れてよい値だけ残す。

    * 取り込み元に無い列は捨てる(押しても黙って消えるのを防ぐため、
      ここで**そもそも受け取らない**)
    * 鍵の列は作るときだけ受ける ── あとから変えられると、
      同じ人が2行に増える
    * 必須の列が空なら断る
    """
    from . import db

    managed = BY_TABLE.get(table)
    if managed is None:
        return {}, Result(False, f"{table} は直せる表ではありません。",
                          REFUSE_NOT_EDITABLE)

    allowed = {c.name: c for c in columns(table, present)}
    cleaned: dict[str, Any] = {}
    for name, raw in (values or {}).items():
        column = allowed.get(str(name))
        if column is None:
            continue
        if column.is_key and not creating:
            continue
        # **列ごとに、直せる/打てるが違う表がある**(端末一覧)。
        # 受け取ってから捨てるのではなく、ここで受け取らない
        if creating and not column.at_create:
            continue
        if not creating and not column.editable:
            continue
        cleaned[column.name] = db.sanitize(raw)

    for column in allowed.values():
        if not column.required or not column.at_create:
            continue
        if creating and not str(cleaned.get(column.name, "")).strip():
            return {}, Result(False, f"{column.name} を入力してください。",
                              REFUSE_BAD_VALUE)
        if not creating and column.name in cleaned \
                and not str(cleaned[column.name]).strip():
            return {}, Result(False, f"{column.name} は空にできません。",
                              REFUSE_BAD_VALUE)
    if managed.validate is not None:
        problem = managed.validate(cleaned, creating)
        if problem:
            return {}, Result(False, problem, REFUSE_BAD_VALUE)
    return cleaned, None


def _check_access(values: dict[str, Any], creating: bool) -> str:
    """アクセス権限の行を確かめる。**値の形を整えるのもここ**(``有効``)。

    * ログインID と PC名 の**両方が空の行は断る** ── 全員への許可になり、
      このツールも別ツールも効かせない。書いた人は効いているつもりに
      なるので、書く前に止める
    * ``有効`` は 1 か 0(空なら 1)。別ツールが数で読むので、数で書く
    """
    if "有効" in values:
        text = str(values.get("有効", "")).strip()
        if text in ("", "1"):
            values["有効"] = 1
        elif text == "0":
            values["有効"] = 0
        else:
            return "有効は 1(有効)か 0(無効)で入れてください。"
    elif creating:
        values["有効"] = 1
    if "権限" in values:
        values["権限"] = str(values["権限"]).strip()
    touches = "ログインID" in values or "PC名" in values
    if creating or touches:
        login = str(values.get("ログインID", "")).strip()
        pc = str(values.get("PC名", "")).strip()
        if (creating or ("ログインID" in values and "PC名" in values)) \
                and not login and not pc:
            return ("ログインID か PC名 の少なくとも一方を入れてください"
                    "(両方空の行は全員への許可になるので効きません)。")
    return ""
