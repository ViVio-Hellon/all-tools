"""マスタ管理(共有 DB のテーブルを画面から見る・足す・直す・消す)

【なぜ要るのか】
Access をやめたことで、**Access を開いて中身を目視する・手で直す**という
経路が無くなりました。資材やサイズを 1 つ足したいだけのときに、いままでは
Access を開けば済んでいました。その受け皿がここです。

**とりわけ「足す」が要ります。** 看板を 1 枚増やすとは、``看板_<ライン>``
に 1 行足すことです。直すだけでは看板を増やせず、増やせないとこの道具だけ
では運用が回りません(結局 Access を開くことになる)。

【パスが無ければ何もできない】
共有 DB の場所が設定されていない、または届かない端末では**開くことも直す
こともできません**。当たり前に見えますが、ここを黙って空表にすると
「マスタが消えた」に見えます。**理由を返して、次に何をすればよいかを言う**
のがこのモジュールの役目のもう半分です。

【読む以外は管理者パスワードが要る】
共有 DB は全端末が見る正式なデータです。足す・直す・消すは、設定画面の
他の保護された操作(モード変更・接続先変更)と同じ関門を通します。

【スキーマが守ってくれない分をここで守る】
Access から変換した ``看板_*`` には **主キーも NOT NULL も UNIQUE も
ありません**(``tools/accdb_to_sqlite.py`` は元の形をそのまま写す)。
つまり ``管理番号`` が重複した行を DB は平気で受け付けます。ところが
書き戻しは ``WHERE 管理番号 = ?`` で行を決めるので、**重複した瞬間に
2 行が同時に書き換わります**。だから重複はここで止めます。
"""

from __future__ import annotations

import math
from dataclasses import asdict, dataclass, field
from typing import Any, Callable

from .. import access_control, applog, config, keys
from ..db.shared import (
    CATEGORY_DATE,
    CATEGORY_NUMBER,
    ROW_KEY,
    SharedDb,
    SharedDbError,
    _ident,
)
from ..domain import models, state

#: 「画面が見ていたキーを送ってこなかった」の印(``None`` は「キーが空の行」なので使えない)
NO_KEY = object()


#: 1 画面に出す行数。共有フォルダの往復を増やさないため、全部は出さない
PAGE_SIZE = 100

#: 直せない理由(画面はこの値で分岐する。文言から推し量らせない)
BLOCK_NO_PATH = "no_path"
BLOCK_MISSING = "missing"
BLOCK_UNREADABLE = "unreadable"

#: 看板を 1 枚足すときに、空欄のままだと困る列。
#:
#: **DB は空欄を許します**(NOT NULL が無い)。それでも止めるのは、
#: ``資材`` が空の看板は画面上ただの空ボタンになり、他の空ボタンと
#: 見分けが付かないためです ── 足した本人にも、どれが足した 1 枚なのか
#: 分からなくなります。
KANBAN_REQUIRED = (config.COL_MATERIAL, config.COL_SIZE)
"""看板で空にできない列(キーは別に常に必須)。

``サイズ`` も要ります。盤は資材とサイズの両方がある行だけを出す
(:meth:`kanban.domain.service.KanbanService.groups`)ので、サイズが空の看板は
**足せたと言われるのに盤に出ません。**
"""

#: 看板の**状態**の列。マスタ管理では**読むだけ**にします。
#:
#: 発注・発送・注文中・倉庫確認は、看板画面のボタンが**組にして**書きます
#: (発注なら ``欲 = 〇`` と ``不 = 空`` と ``更新日`` を同時に。
#: :mod:`kanban.domain.service`)。ここで 1 マスだけ直すと、その組が崩れて
#: 「欲も不も〇」のような、ボタンでは作れない看板ができます。しかも各端末の
#: 手元には未反映の操作が残っていることがあり、あとからそれが上書きします。
#: 状態を変えたいときは、看板画面のボタンで操作してもらいます。
STATE_COLUMNS = state.STATE_COLUMNS          # kanban/domain/state.py(1 か所で決める)

#: 看板を足すときの状態。**選ばせずに、これで固定します**(:data:`kanban.domain.state.INITIAL_STATE`)。
#:
#: 新しい看板は「まだ発注していない」= ``不 = 〇``、ほかは空から始まります
#: (看板画面で発注を取り消したあとと同じ形)。
KANBAN_INITIAL_STATE = state.INITIAL_STATE

#: 印の欄に入れてよい値(状態の列は上で読むだけにしたので、残るのは常設品)。
#:
#: アプリは印を **1 文字で**見分けます(``常設品 == "×"`` なら非常設品。
#: :class:`kanban.domain.models.KanbanItem`)。見た目の似た別の字 ── 英字の
#: ``x`` や ``X``、IME で「まる」と打つと出る ``○``(白丸)── が入ると、
#: **その印は無いものとして扱われ**、画面では何も起きていないように見えます。
#: だから入口で断ります。
MARK_VALUES = {
    config.COL_PERMANENT: ("", config.MARK_ON, config.MARK_NON_PERMANENT),
}

#: 看板を足すときの初期値(画面に**入れて見せる**。黙って足さない)。
#:
#: ``常設品 = 〇``(= 常設)を既定にしておき、非常設なら ``×`` に直して
#: もらいます(``config.MARK_NON_PERMANENT``)。状態の列は
#: :data:`KANBAN_INITIAL_STATE` で固定なので、ここには入れません。
KANBAN_DEFAULTS = {
    config.COL_PERMANENT: config.MARK_ON,
}


def _state_message(column: str) -> str:
    return (
        f"「{column}」は看板の状態です。マスタ管理では直せません。"
        "発注・発送・注文中は、看板画面のボタンで操作してください。"
    )


@dataclass
class TableInfo:
    name: str
    rows: int = 0
    kind: str = "other"
    """``kanban``(看板_<ライン>)/ ``state``(Form状態管理)/ ``access``(梱包資材マスタの
    アクセス権限)/ ``other``。"""


@dataclass
class ColumnInfo:
    """1 列の見た目と決まりごと(画面が入力欄を組むのに使う)。"""

    name: str
    category: str = ""
    """``NUMBER`` / ``DATE`` / ``TEXT``。``SharedDb`` の宣言型から。"""

    is_key: bool = False
    """キー列(足すときは必須、直すときは変えられない)。"""

    required: bool = False
    """空欄で足せない列。"""

    state: bool = False
    """看板の状態の列(読むだけ。足すときも :data:`KANBAN_INITIAL_STATE` で固定)。"""


@dataclass
class MasterView:
    """マスタ管理の面。"""

    available: bool = False
    """開けるか。False なら ``blocked`` と ``why`` に理由が入る。"""

    blocked: str = ""
    why: str = ""
    path: str = ""
    tables: list[TableInfo] = field(default_factory=list)

    # 表を 1 つ開いているときだけ入る
    table: str = ""
    view_only_why: str = ""
    """直せない表なら、その理由。空なら直せる。**隠さずに理由を出す。**"""

    row_key: str = ""
    """行を指す隠し列の名前(``__行``)。画面はこれを書き換え・削除に送る。"""

    columns: list[str] = field(default_factory=list)
    column_info: list[ColumnInfo] = field(default_factory=list)
    key_column: str = ""
    rows: list[dict[str, Any]] = field(default_factory=list)
    total: int = 0
    page: int = 0
    pages: int = 0

    # 行を足すための材料(画面の入力欄の初期値)
    next_key: str = ""
    """空いている次のキー。看板は連番なので当てられる。当てられなければ空。"""

    defaults: dict[str, str] = field(default_factory=dict)
    """足すときの初期値。**画面に入れて見せる**(黙って足さない)。"""

    fixed: dict[str, str] = field(default_factory=dict)
    """足すときに固定で入る値(看板の状態)。画面は読むだけの欄として見せる。"""

    db: str = ""
    """どのファイルか。``""`` = 共有DB(看板マスタ)/ ``access`` = 梱包資材マスタ。"""

    db_label: str = "共有DB"

    note: str = ""
    """表の上に出す説明(アクセス権限の読み方など)。"""

    choices: dict[str, list[str]] = field(default_factory=dict)
    """列ごとの入力候補(画面は選択肢として出す。**候補以外も入れられる**)。"""

    hints: dict[str, str] = field(default_factory=dict)
    """列ごとの一言(入力欄の下に出す)。"""

    sort: str = ""
    """並べ替えている列(空ならファイルの順)。"""

    sort_dir: str = "asc"
    """``asc``(小さい順)/ ``desc``(大きい順)。"""


def _is_access(shared: Any) -> bool:
    return getattr(shared, "role", "") == access_control.AccessDb.role


def _db_label(shared: Any) -> str:
    return "梱包資材マスタ" if _is_access(shared) else "共有DB"


def _classify(name: str, shared: Any = None) -> str:
    # 梱包資材マスタは**アクセス権限の表だけ**を扱う(ほかは python-web-tools の表)
    if _is_access(shared):
        return "access" if name == access_control.TABLE else "other"
    if name.startswith(config.KANBAN_TABLE_PREFIX):
        return "kanban"
    if name == config.TABLE_STATE:
        return "state"
    return "other"


#: アクセス権限の表の上に出す説明。**ほかのツールも同じ表を読む**ことを先に言う
ACCESS_NOTE = (
    "梱包資材マスタの「アクセス権限」です。1 行 = 1 つの許可で、ログインID と PC名 の"
    "当てはまる行の権限をすべて使えます(空欄は「問わない」。両方空の行は誰にも効きません)。"
    "このツールが読むのは mode:field(現場モード)と mode:material(倉庫モード)だけで、"
    "ほかの文字列は python-web-tools など、ほかのツールの権限です(消さないでください)。"
    "当てはまる行が無い端末は現場モードだけ使えます。行が 1 つでも当てはまると、その行の"
    "モードだけになります ── 倉庫の人・端末が現場モードも使うなら mode:field の行も足してください。"
)

ACCESS_HINTS = {
    access_control.COL_LOGIN: "Windows のログインID。空なら誰でも(PC名で絞る)",
    access_control.COL_PC: "PC名(コンピューター名)。空ならどのPCでも(ログインIDで絞る)",
    access_control.COL_PERMISSION: (
        "mode:field = 現場モード / mode:material = 倉庫モード / ライン名(L-1・LVC・HVC・機側・NS1・AIM)"
        " = 担当ライン。ほかのツールの権限も入れられます"),
    access_control.COL_ENABLED: "1 = 有効 / 0 = 無効(空欄も有効)",
}


#: 直せない表と、その理由。**出さないのではなく、理由を出す。**
#:
#: 隠すと、探している人には**画面が壊れて見えます**(「あるはずの表が無い」)。
#: 出したうえで「なぜここでは直せないのか」「では、どこで直すのか」を言います。
VIEW_ONLY_WHY = {
    config.TABLE_STATE: (
        "端末が開いているかどうかを、各端末が自動で書き込む表です。"
        "手で直しても、次に書き込まれた時点で元に戻ります。"
    ),
    "看板履歴": (
        "以前の看板履歴の置き場所です。いまは看板履歴.sqlite3(「接続先」タブの"
        "「看板履歴の置き場所」)へ書き、起動するたびにここにある行のうち、まだ写していないものを"
        "写します。手で直しても集計には効きません。"
    ),
    "看板コメント": (
        "倉庫と現場が看板の画面の 💬 で書いたやり取りと、届いたときの片付けの印です。"
        "看板の画面から書いてください(ここで直すと、各端末の手元の写しと食い違います)。"
    ),
}

#: 一覧に無い表の既定の理由。**知らない表は触らせない。**
#: この道具が面倒を見ているのは ``看板_*`` だけで、それ以外は何のための
#: 表か分からないまま書き換えることになります。
DEFAULT_VIEW_ONLY = (
    "この道具が直す表ではありません。中身の確認だけできます。"
)


def view_only_why(table: str, shared: Any = None) -> str:
    """その表を直せない理由。直せる表なら空文字。

    直せるのは ``看板_<ライン>`` と、梱包資材マスタの ``アクセス権限`` だけです
    ── 看板を増やす・減らす・資材やサイズを直す、誰がどのモードを使えるかを
    決める、のがこの画面の役目だからです。
    """
    if _classify(table, shared) in ("kanban", "access"):
        return ""
    return VIEW_ONLY_WHY.get(table, DEFAULT_VIEW_ONLY)


def editable(table: str, shared: Any = None) -> bool:
    return not view_only_why(table, shared)


def frame(shared: SharedDb | None, *, configured: bool = True) -> MasterView:
    """テーブルの一覧だけを作る(表の中身は読まない)。

    設定を 1 つ変えたいだけの人まで、共有フォルダの往復を待たせないため。
    中身はその表を開いたときに読みます。
    """
    view = MasterView(path=shared.path if shared else "",
                      db="access" if _is_access(shared) else "", db_label=_db_label(shared))

    blocked, why = availability(shared, configured=configured)
    if blocked:
        view.blocked, view.why = blocked, why
        return view

    assert shared is not None
    try:
        names = shared.table_names()
    except SharedDbError as exc:
        view.blocked, view.why = BLOCK_UNREADABLE, str(exc)
        return view

    view.available = True
    view.tables = [TableInfo(name=n, kind=_classify(n, shared)) for n in names]
    return view


def availability(
    shared: SharedDb | None, *, configured: bool = True
) -> tuple[str, str]:
    """開けるか。開けないなら ``(種別, 理由)``。開けるなら ``("", "")``。

    **パスが無ければ、そこで止める。** 表を出してから「保存できません」と
    言うより、開く前に言うほうが早い。

    ``configured`` は「設定画面で明示的に設定されているか」。設定が空でも
    ``config`` は既定の置き場所へ落とすので、届かない理由が「設定していない」
    のか「設定したが届かない」のかを、ここで言い分ける ── 直し方が違う。
    """
    if shared is None or not str(shared.path).strip():
        return BLOCK_NO_PATH, (
            "共有DBの場所が設定されていません。"
            "「接続先」で設定してから開いてください。"
        )
    if _is_access(shared) and not shared.exists():
        return BLOCK_MISSING, (
            f"梱包資材マスタが見つかりません: {shared.path}\n"
            "共有フォルダに届いているか、「接続先」タブの「アクセス権限の置き場所」が"
            "正しいかを確認してください。"
        )
    if not shared.exists():
        if not configured:
            return BLOCK_NO_PATH, (
                "共有DBの場所が設定されていません。"
                f"既定の場所を見ましたが、そこにもありません: {shared.path}\n"
                "「接続先」で設定してください。"
            )
        return BLOCK_MISSING, (
            f"共有DBが見つかりません: {shared.path}\n"
            "共有フォルダに届いているか、接続先が正しいかを確認してください。"
        )
    return "", ""


def _sort_key(value: Any, category: str) -> tuple:
    """並べ替えの鍵。**数は数として、日時は日時として**比べる(文字の順だと 10 が 9 より前、
    VBA が書いたゼロ埋めなしの日時 ``2026/9/1`` が ``2026/10/1`` より後になる)。
    数に読めない値は文字として、数のあとに並べる。"""
    text = "" if value is None else str(value).strip()
    if category == CATEGORY_DATE:
        at = models.parse_datetime(text)
        if at is not None:
            return (0, at.timestamp(), "")
    try:
        number = float(text)
        if math.isfinite(number):
            return (0, number, "")
    except ValueError:
        pass
    return (1, 0.0, text)


def sort_rows(rows: list[dict[str, Any]], column: str, category: str, descending: bool) -> list[dict[str, Any]]:
    """``column`` で並べ替える。**空欄は向きにかかわらず最後**(探している値が空欄に埋もれない)。
    同じ値どうしはファイルの順のまま。"""
    def blank(r: dict[str, Any]) -> bool:
        return r.get(column) is None or str(r.get(column)).strip() == ""

    filled = [r for r in rows if not blank(r)]
    empty = [r for r in rows if blank(r)]
    filled.sort(key=lambda r: _sort_key(r.get(column), category), reverse=descending)
    return filled + empty


def open_table(shared: SharedDb | None, table: str, page: int = 0, *,
               sort: str = "", sort_dir: str = "asc") -> MasterView:
    """表を 1 つ開く。``sort`` の列で**表全体を**並べ替えてからページに分ける。"""
    view = frame(shared)
    if not view.available:
        return view

    assert shared is not None
    known = {t.name for t in view.tables}
    if table not in known:
        # **一覧に無いものは開かせない**(画面で絞るだけでなくここでも確かめる)
        view.blocked, view.available, view.why = (
            BLOCK_UNREADABLE,
            False,
            f"そのテーブルはありません: {table}",
        )
        return view

    try:
        # **行を指すのに rowid を添える。** 管理番号は重複しうるので、
        # それで指すと 1 回の書き換えが 2 行に当たる
        snapshot = shared.read_table(table, with_row_key=True)
    except SharedDbError as exc:
        view.blocked, view.available, view.why = BLOCK_UNREADABLE, False, str(exc)
        return view

    kind = _classify(table, shared)
    view.table = table
    view.row_key = ROW_KEY
    view.view_only_why = view_only_why(table, shared)
    view.columns = snapshot.columns
    view.key_column = _key_column(snapshot.columns)
    view.column_info = _describe_columns(table, snapshot.columns, snapshot.categories, kind)
    view.next_key = _suggest_key(snapshot.rows, view.key_column)
    view.defaults = _defaults_for(table, snapshot.columns, kind)
    view.fixed = _fixed_for(table, snapshot.columns, kind)
    if kind == "access":
        view.note = ACCESS_NOTE
        view.hints = {c: h for c, h in ACCESS_HINTS.items() if c in snapshot.columns}
        # 候補は**このツールの 2 つ + 表にすでにある権限**(ほかのツールの権限を打ち直さずに済む)
        seen = [str(r.get(access_control.COL_PERMISSION) or "").strip() for r in snapshot.rows]
        from .. import line_names

        names = [n["official"] for n in line_names.table_rows() if n["supported"]]
        others = sorted({s for s in seen if s and s not in access_control.KNOWN and s not in names})
        view.choices = {access_control.COL_PERMISSION:
                        [access_control.PERM_FIELD, access_control.PERM_MATERIAL, *names, *others]}
    rows = snapshot.rows
    if sort in snapshot.columns:
        view.sort, view.sort_dir = sort, ("desc" if sort_dir == "desc" else "asc")
        rows = sort_rows(rows, sort, snapshot.categories.get(sort, ""), view.sort_dir == "desc")
    view.total = len(rows)
    view.pages = max(1, (view.total + PAGE_SIZE - 1) // PAGE_SIZE)
    view.page = max(0, min(int(page), view.pages - 1))
    start = view.page * PAGE_SIZE
    view.rows = rows[start : start + PAGE_SIZE]
    for info in view.tables:
        if info.name == table:
            info.rows = view.total
    return view


def _key_column(columns: list[str]) -> str:
    """更新の ``WHERE`` に使う列。

    看板テーブルは ``管理番号``。無ければ先頭列に戻す(VBA が先頭列を
    キーとして扱っていたのと同じ最後の手)。
    """
    if config.COL_KEY in columns:
        return config.COL_KEY
    return columns[0] if columns else ""


def _required_columns(table: str, columns: list[str], kind: str | None = None) -> set[str]:
    """空欄では足せない列。**キーは常に必須。**

    キーが無い行は、書き戻し(``WHERE キー``)から永久に外れます ── 画面
    には出るのに誰も直せない行になるので、作らせません。
    """
    kind = kind or _classify(table)
    required = {_key_column(columns)} - {""}
    if kind == "kanban":
        required |= {c for c in KANBAN_REQUIRED if c in columns}
    if kind == "access":
        # 権限の無い行は何も許さない(実物の表も NOT NULL)
        required |= {access_control.COL_PERMISSION} & set(columns)
    return required


def _describe_columns(
    table: str, columns: list[str], categories: dict[str, str], kind: str | None = None
) -> list[ColumnInfo]:
    key = _key_column(columns)
    required = _required_columns(table, columns, kind)
    state = _state_columns(table, columns, kind)
    return [
        ColumnInfo(
            name=name,
            category=categories.get(name, ""),
            is_key=name == key,
            required=name in required,
            state=name in state,
        )
        for name in columns
    ]


def _state_columns(table: str, columns: list[str], kind: str | None = None) -> set[str]:
    """その表で読むだけにする状態の列(看板の表だけ)。"""
    if (kind or _classify(table)) != "kanban":
        return set()
    return {c for c in STATE_COLUMNS if c in columns}


def _defaults_for(table: str, columns: list[str], kind: str | None = None) -> dict[str, str]:
    kind = kind or _classify(table)
    if kind == "access":
        # **この端末の ID と PC名を入れて見せる。** 権限を足すのはたいてい
        # その端末の前で「この端末を倉庫にしたい」とき。別の端末なら書き換える
        me = access_control.current_identity()
        values = {access_control.COL_LOGIN: me.login_id, access_control.COL_PC: me.pc_name,
                  access_control.COL_ENABLED: "1"}
        return {k: v for k, v in values.items() if k in columns}
    if kind != "kanban":
        return {}
    return {k: v for k, v in KANBAN_DEFAULTS.items() if k in columns}


def _fixed_for(table: str, columns: list[str], kind: str | None = None) -> dict[str, str]:
    state = _state_columns(table, columns, kind)
    return {k: v for k, v in KANBAN_INITIAL_STATE.items() if k in state}


def _suggest_key(rows: list[dict[str, Any]], key_column: str) -> str:
    """次のキーを当てる(``管理番号`` の最大 + 1)。

    **当てるだけ。** 画面では直せますし、送られてきた値をそのまま使います
    ── ここで決め打つと、連番でない運用(枝番など)を塞いでしまいます。
    数字でないキーが混ざっていれば、当てずに空を返します。
    """
    if not key_column:
        return ""
    best: int | None = None
    for row in rows:
        value = row.get(key_column)
        # **空の行は飛ばす。** 変換した表には管理番号が NULL の行が混ざり得る。
        # ``str(None)`` は ``"None"`` になり、以前はそれを「数字でないキー」と
        # 取り違えて、1 行あるだけで提案そのものを止めていた
        raw = "" if value is None else str(value).strip()
        if not raw:
            continue
        try:
            number = float(raw)
        except ValueError:
            return ""
        if not math.isfinite(number):
            continue
        best = int(number) if best is None else max(best, int(number))
    return str((best or 0) + 1)


def _coerce(value: Any, category: str) -> tuple[Any, str]:
    """画面から来た値を、共有 DB へ入れる形にする。

    戻り値は ``(値, 断る理由)``。理由が空でなければ入れません。

    **空欄は空文字のまま**にします(NULL にしません)。Access から変換した
    ファイルがそうなっているためで、ここだけ NULL を混ぜると、SQLite を
    直接覗いた人に「この行だけ何か違う」と読ませてしまいます。数値列の
    空欄だけは NULL にします ── 数値列に入った空文字は、数でも空欄でも
    ない中途半端な値になるためです。
    """
    text = "" if value is None else str(value).strip()
    if category == CATEGORY_NUMBER:
        if not text:
            return None, ""
        try:
            number = float(text)
        except ValueError:
            return None, "数字で入れてください"
        if not math.isfinite(number):
            # ``float()`` は "nan" や "inf" も数として受け付けてしまう。nan は
            # SQLite で NULL になり、**キーの無い行**ができていた
            return None, "数字で入れてください"
        return int(number) if number.is_integer() else number, ""
    if category == CATEGORY_DATE:
        # **形を確かめるだけで、直さない。** 打ち間違いを黙って別の日時に
        # しないため。受け付ける書き方は読む側と同じ(`parse_datetime` は
        # VBA 版が書いたゼロ埋めなしも通す)
        if text and models.parse_datetime(text) is None:
            return None, "日時です。2026/08/31 09:00:00 の形で入れてください"
        return text, ""
    return text, ""


@dataclass
class EditResult:
    ok: bool
    message: str = ""
    reason: str = ""
    key: str = ""
    """消した・足した行の業務のキー(看板なら管理番号)。後始末に使う。"""


def _write(shared: SharedDb, statements) -> tuple[list | None, EditResult | None]:
    """共有DBへ書く。**開けなかったら理由を返す**(例外で落とさない)。

    ``SharedDb.execute`` は、共有DBを開くところで失敗すると ``SharedDbError``
    を投げます(1 文ずつの失敗は結果として返るが、開けなければ文まで行かない)。
    それを素通しにすると画面には「通信に失敗しました (500)」としか出ず、
    **共有フォルダに届いていないのか、アプリが壊れたのか**が読めません。
    """
    try:
        return shared.execute(statements), None
    except SharedDbError as exc:
        applog.error("マスタを書けませんでした: %s", exc)
        return None, EditResult(
            False,
            f"{_db_label(shared)}に書けませんでした。共有フォルダに届いているか確かめてください。({exc})",
            "unreachable",
        )


def _mark_message(column: str, value: str) -> str:
    if column == config.COL_PERMANENT:
        return (
            f"「{column}」は {config.MARK_ON}(常設)か {config.MARK_NON_PERMANENT}(非常設)です"
            f"(いま「{value}」)。× は「ばつ」で出る字です。"
        )
    return (
        f"「{column}」は {config.MARK_ON} か空欄です(いま「{value}」)。"
        f"{config.MARK_ON} は「ぜろ」で出る漢数字の〇で、「まる」で出る ○ とは別の字です。"
    )


def _kanban_problem(
    table: str,
    snapshot,
    after: dict[str, Any],
    columns: list[str],
    *,
    self_row_key: Any = None,
    kind: str | None = None,
) -> "EditResult | None":
    """足した・直したあとの行が、**看板として盤に出る形か**を確かめる。

    ``columns`` は確かめる列(足すときは全部、直すときは直した列だけ)。
    直すときに他の列まで見ると、昔から入っている変わった値のせいで、
    関係のない修正まで断ることになるためです。

    確かめること(看板の表だけ):

    * **印の欄**は決まった 1 文字か(:data:`MARK_VALUES`)
    * **資材・サイズ**が空でないか(空だと盤に出ない)
    * **同じ資材・同じサイズ**の看板が他に無いか。盤は資材ごとにサイズの
      重複を 1 枚にまとめる(VBA と同じ)ので、2 枚目は**足せたのに出ない**

    アクセス権限の表なら :func:`_access_problem`。
    """
    kind = kind or _classify(table)
    if kind == "access":
        return _access_problem(snapshot, after, self_row_key=self_row_key)
    if kind != "kanban":
        return None

    for column in columns:
        allowed = MARK_VALUES.get(column)
        if allowed is None:
            continue
        value = "" if after.get(column) is None else str(after[column]).strip()
        if value not in allowed:
            return EditResult(False, _mark_message(column, value), "bad_mark")

    for column in KANBAN_REQUIRED:
        if column in columns and column in snapshot.columns:
            if not str(after.get(column) or "").strip():
                return EditResult(
                    False, f"「{column}」は空にできません(空の看板は盤に出ません)。", "required"
                )

    watched = {config.COL_MATERIAL, config.COL_SIZE}
    if watched & set(columns) and watched <= set(snapshot.columns):
        material = str(after.get(config.COL_MATERIAL) or "").strip()
        size = str(after.get(config.COL_SIZE) or "").strip()
        key_column = _key_column(snapshot.columns)
        for other in snapshot.rows:
            if self_row_key is not None and str(other.get(ROW_KEY, "")) == str(self_row_key):
                continue
            if (str(other.get(config.COL_MATERIAL) or "").strip() == material
                    and str(other.get(config.COL_SIZE) or "").strip() == size):
                return EditResult(
                    False,
                    f"「{material} / {size}」の看板はすでにあります"
                    f"({key_column} {other.get(key_column)})。"
                    "同じ資材・同じサイズの看板は、盤に 1 枚しか出ません。",
                    "duplicate_kanban",
                )
    return None


def _access_problem(snapshot, after: dict[str, Any], *, self_row_key: Any = None) -> "EditResult | None":
    """アクセス権限の 1 行として**効く形か**を確かめる。

    * ``権限`` が空でないか(実物の表も NOT NULL。何も許さない行になる)
    * ``ログインID`` と ``PC名`` の**両方が空でないか** ── 両方空の行は全員への許可に
      なるので、このツールも python-web-tools も**効かせない**。足せたのに効かない行を作らない
    * **同じ行がすでに無いか**(ID・PC名・権限が同じ。大文字小文字は区別しない)

    権限の文字列そのものは断らない ── **ほかのツールの権限も入る表**なので。
    """
    ac = access_control

    def text(row: dict[str, Any], column: str) -> str:
        return "" if row.get(column) is None else str(row.get(column)).strip()

    permission = text(after, ac.COL_PERMISSION)
    if ac.COL_PERMISSION in snapshot.columns and not permission:
        return EditResult(False, f"「{ac.COL_PERMISSION}」は空にできません"
                                 f"(例: {ac.PERM_FIELD} / {ac.PERM_MATERIAL})。", "required")
    login, pc = text(after, ac.COL_LOGIN), text(after, ac.COL_PC)
    if not login and not pc:
        me = ac.current_identity()
        return EditResult(
            False,
            f"「{ac.COL_LOGIN}」と「{ac.COL_PC}」の両方を空にはできません(全員への許可になるので、"
            "このツールもほかのツールも効かせません)。どちらか一方でも入れてください"
            f"(この端末なら {ac.COL_LOGIN} = {me.login_id} / {ac.COL_PC} = {me.pc_name})。",
            "no_condition",
        )
    key_column = _key_column(snapshot.columns)
    for other in snapshot.rows:
        if self_row_key is not None and str(other.get(ROW_KEY, "")) == str(self_row_key):
            continue
        if (text(other, ac.COL_LOGIN).casefold() == login.casefold()
                and text(other, ac.COL_PC).casefold() == pc.casefold()
                and text(other, ac.COL_PERMISSION).casefold() == permission.casefold()):
            return EditResult(
                False,
                f"同じ行がすでにあります({key_column} {_key_text(other.get(key_column))}: "
                f"{login or '(空)'} / {pc or '(空)'} / {permission})。"
                "無効にしてあるなら、その行の「有効」を 1 にしてください。",
                "duplicate_rule",
            )
    return None


def _access_note(after: dict[str, Any]) -> str:
    """足した・直した権限が**このツールでどう効くか**の一言(断らない。知らせるだけ)。"""
    code = "" if after.get(access_control.COL_PERMISSION) is None else str(
        after.get(access_control.COL_PERMISSION)).strip()
    if not code:
        return ""
    if code in access_control.KNOWN:
        return f" {code} = {access_control.PERMISSION_LABEL[code]}。"
    from .. import line_names

    parts = []
    for token in line_names.tokens(code):
        d = line_names.by_official(token)
        if d is not None:
            parts.append(f"{token} = 担当ライン「{d.official}」" + ("" if line_names.supported(d)
                         else "(このツールに看板が無いので、担当ラインにはしません)"))
            continue
        clue = line_names.hint(token)
        if clue:
            parts.append(f"※「{token}」はラインとしては読みません({clue})")
    if parts:
        return " " + "。".join(parts) + "。"
    near = access_control.near_known(code)
    if near:
        return (f" ※「{code}」はこのツールでは効きません。{near} の打ち間違いなら直してください"
                "(ほかのツールの権限なら、このままで構いません)。")
    return f" ※「{code}」はこのツールでは使いません(ほかのツールの権限として残ります)。"


def update_cell(
    shared: SharedDb | None,
    table: str,
    row_key: Any,
    column: str,
    value: Any,
    *,
    configured: bool = True,
    expect_key: Any = NO_KEY,
    expect_before: Any = NO_KEY,
) -> EditResult:
    """1 マスだけ直す。

    ``expect_before`` は画面がそのマスに出していた値。**開いたあとに別の端末が同じ
    マスを変えていたら、上書きせずに断る**(後から直した人が、先に直した人の値を
    知らないまま消していた)。

    **表ごと保存しない。** 画面が持っている古い行をまとめて書き戻すと、
    その間に他端末が変えた分を巻き込んで消します。直したマスだけを送ります。

    ``row_key`` は :data:`kanban.db.shared.ROW_KEY` の値(``rowid``)です。
    業務のキーで指さないのは、**そのキーが重複した表で 2 行に当たる**のを
    避けるためです。
    """
    snapshot, refused = _gate(shared, table, configured=configured)
    if refused is not None:
        return refused
    assert shared is not None and snapshot is not None

    kind = _classify(table, shared)
    blocked = view_only_why(table, shared)
    if blocked:
        return EditResult(False, blocked, "view_only")

    if column not in snapshot.columns:
        return EditResult(False, f"その列はありません: {column}", "no_column")
    key_column = _key_column(snapshot.columns)
    if column == key_column:
        # キーを書き換えると、どの行だったのかが辿れなくなる
        return EditResult(False, f"{key_column} は変更できません。", "key_readonly")
    if column in _state_columns(table, snapshot.columns, kind):
        return EditResult(False, _state_message(column), "state_readonly")

    where, refused = _where_row(snapshot, row_key, expect_key)
    if refused is not None:
        return refused

    clean, problem = _coerce(value, snapshot.categories.get(column, ""))
    if problem:
        return EditResult(False, f"「{column}」は{problem}", "bad_value")

    # **足すときと同じ決まりで確かめる。** 以前は足すときだけ確かめていて、
    # 直せば資材を空にも、印に ○ を入れることもできた(看板が盤から消える)
    current = next(
        r for r in snapshot.rows if str(r.get(ROW_KEY, "")).strip() == str(row_key).strip()
    )
    if expect_before is not NO_KEY and not _same_key(current.get(column), expect_before):
        shown = "" if current.get(column) is None else _key_text(current.get(column))
        return EditResult(
            False,
            f"開いたあとに別の端末が「{column}」を「{shown}」に変えています。"
            "画面を開き直して、いまの値を確かめてから直してください。",
            "not_found",
        )
    refused = _kanban_problem(
        table, snapshot, {**current, column: clean}, [column], self_row_key=row_key, kind=kind
    )
    if refused is not None:
        return refused

    # **読んだときの値のままのときだけ書く**(読んでから書くまでのあいだに変わって
    # いたら 0 件になり、下で「開き直してください」と断る)
    params: list[Any] = [clean, *where[1]]
    guard = ""
    if expect_before is not NO_KEY:
        guard = f" AND {_ident(column)} IS ?"
        params.append(current.get(column))
    sql = f"UPDATE {_ident(table)} SET {_ident(column)} = ? WHERE {where[0]}{guard}"
    results, refused = _write(shared, [(sql, params)])
    if refused is not None:
        return refused
    result = results[0]
    if not result.ok:
        applog.error("マスタ更新に失敗: %s", result.error_message)
        return EditResult(False, f"更新できませんでした: {result.error_message}", "failed")
    if result.affected == 0:
        # 他端末が消したか、キーが変わった
        return EditResult(
            False,
            "対象の行が見つかりませんでした。画面を開き直してください。",
            "not_found",
        )

    applog.info("マスタを更新: %s.%s (%s=%s)", table, column, ROW_KEY, row_key)
    note = _access_note({**current, column: clean}) if kind == "access" else ""
    return EditResult(True, "更新しました。" + note)


def insert_row(
    shared: SharedDb | None,
    table: str,
    values: dict[str, Any],
    *,
    configured: bool = True,
) -> EditResult:
    """1 行足す。**看板を 1 枚増やすのがこれ。**

    足す前に確かめること:

    * キー(``管理番号``)が入っているか ── 書き戻しは ``WHERE キー`` で
      行を決めるので、キーの無い行は**あとから誰も直せません**
    * そのキーが**すでに無いか** ── スキーマに UNIQUE が無いので、
      DB は重複を受け付けてしまう。重複すると 1 回の書き戻しで 2 行が
      同時に変わる
    * 看板なら ``資材`` ``サイズ`` が入っているか(:data:`KANBAN_REQUIRED`)
    * 看板の状態は選ばせず、:data:`KANBAN_INITIAL_STATE` で足す
    """
    snapshot, refused = _gate(shared, table, configured=configured)
    if refused is not None:
        return refused
    assert shared is not None and snapshot is not None

    kind = _classify(table, shared)
    blocked = view_only_why(table, shared)
    if blocked:
        return EditResult(False, blocked, "view_only")

    key_column = _key_column(snapshot.columns)
    if not key_column:
        return EditResult(False, "キー列が分かりません。", "no_key")

    unknown = [c for c in values if c not in snapshot.columns]
    if unknown:
        return EditResult(
            False, f"その列はありません: {'、'.join(unknown)}", "no_column"
        )

    # **状態は選ばせない。** 空欄は「指定なし」として初期の状態で入れる
    # (古い画面は状態の欄を空で送ってくる。それで何も足せなくなっていた)。
    # 空でない値が初期の状態と違うときだけ断る ── 黙って捨てると、入れた
    # つもりの値が消えたように見える
    fixed = _fixed_for(table, snapshot.columns, kind)
    for column, initial in fixed.items():
        sent = "" if values.get(column) is None else str(values[column]).strip()
        if sent and sent != initial:
            return EditResult(False, _state_message(column), "state_readonly")

    required = _required_columns(table, snapshot.columns, kind)
    row: dict[str, Any] = {}
    for column in snapshot.columns:
        if column in fixed:
            row[column] = fixed[column]
            continue
        clean, problem = _coerce(
            values.get(column, ""), snapshot.categories.get(column, "")
        )
        if problem:
            return EditResult(False, f"「{column}」は{problem}", "bad_value")
        if column in required and (clean is None or str(clean).strip() == ""):
            why = (
                "(空の看板は盤に出ません)" if column in KANBAN_REQUIRED
                else "(キーが無い行は、あとから誰も直せません)" if column == key_column
                else ""
            )
            return EditResult(False, f"「{column}」は空にできません{why}。", "required")
        row[column] = clean

    refused = _kanban_problem(table, snapshot, row, list(row), kind=kind)
    if refused is not None:
        return refused

    key_value = row[key_column]
    if _has_key(snapshot.rows, key_column, key_value):
        return EditResult(
            False,
            f"{key_column} {key_value} はすでにあります。別の番号にしてください。",
            "duplicate_key",
        )

    # **重複は SQL 自身にも断らせる。**
    #
    # 上の確認は「読んでから書く」ので、読んだあと・書く前に他端末が同じ
    # 番号を入れると擦り抜けます。UNIQUE が無い以上、DB は止めてくれない
    # ので、``WHERE NOT EXISTS`` を付けて 1 文の中で確かめます
    # (入らなかったことは ``affected == 0`` で分かる)。
    columns = list(row)
    sql = (
        f"INSERT INTO {_ident(table)} "
        f"({', '.join(_ident(c) for c in columns)}) "
        f"SELECT {', '.join('?' for _ in columns)} "
        f"WHERE NOT EXISTS ("
        f"SELECT 1 FROM {_ident(table)} WHERE {_ident(key_column)} = ?)"
    )
    results, refused = _write(shared, [(sql, [row[c] for c in columns] + [key_value])])
    if refused is not None:
        return refused
    result = results[0]
    if not result.ok:
        applog.error("マスタ追加に失敗: %s", result.error_message)
        return EditResult(False, f"追加できませんでした: {result.error_message}", "failed")
    if result.affected == 0:
        # 読んだあと・書く前に、他端末が同じ番号を入れた
        return EditResult(
            False,
            f"{key_column} {key_value} はすでにあります。別の番号にしてください。",
            "duplicate_key",
        )

    applog.info("マスタに追加: %s (%s=%s)", table, key_column, key_value)
    note = _access_note(row) if kind == "access" else ""
    return EditResult(True, f"{table} に 1 行足しました({key_column} {key_value})。" + note,
                      key=_key_text(key_value))


def delete_row(
    shared: SharedDb | None,
    table: str,
    row_key: Any,
    *,
    configured: bool = True,
    expect_key: Any = NO_KEY,
    unsent: Callable[[str], bool] | None = None,
) -> EditResult:
    """1 行消す。

    ``unsent(管理番号)`` は、この端末にその看板の**まだ共有DBへ届いていない操作**が
    あるか(:meth:`kanban.db.store.Store.has_unsent`)。あれば消さない。

    **足せるなら消せないと困ります** ── 番号を打ち間違えて足した行を、
    直す手段だけでは取り除けません(キー列は直せないため)。

    消せるのは 1 行ずつです。まとめて消す口は作りません ── 取り返しの
    つかない操作の幅は、狭いほうが安全です。``row_key`` で指すのも同じ
    理由で、業務のキーで指すと**重複した表では 2 行消えます。**
    """
    snapshot, refused = _gate(shared, table, configured=configured)
    if refused is not None:
        return refused
    assert shared is not None and snapshot is not None

    blocked = view_only_why(table, shared)
    if blocked:
        return EditResult(False, blocked, "view_only")

    where, refused = _where_row(snapshot, row_key, expect_key)
    if refused is not None:
        return refused
    target = next(r for r in snapshot.rows if str(r.get(ROW_KEY, "")).strip() == str(row_key).strip())
    key_column = _key_column(snapshot.columns)
    key_text = _key_text(target.get(key_column)) if key_column else ""

    # **動いている看板は消さない。** 発注中・発送済み・注文中の看板を消すと、
    # 倉庫はその注文を見失い、現場の赤も行き場を失う(実機テストで見つけた)
    guard = ""
    is_kanban = _classify(table, shared) == "kanban"
    if is_kanban:
        busy = _busy_state(target)
        if busy:
            return EditResult(
                False,
                f"{key_column} {key_text} は{busy}です。消すと、その注文の行き場が無くなります。"
                "先に看板の画面で片付けて(届いたら赤を消す・倉庫は発送や注文中を外す)から消してください。",
                "in_use",
            )
        # **この端末から送っていない発注があれば消さない。** 共有DBの行はまだ空でも、
        # 押した赤がこの端末に預かったまま。消すと送り先が無くなり、発注ごと捨てられる
        if unsent is not None and key_text and unsent(key_text):
            return EditResult(
                False,
                f"{key_column} {key_text} には、この端末からまだ共有DBへ届いていない操作(発注など)があります。"
                "届いてから(看板画面の「未反映」が 0 になってから)、看板の画面で片付けて消してください。",
                "in_use",
            )
        # **消す瞬間にも確かめる。** 読んでから消すまでのあいだに、ほかの端末の発注が
        # 届いていたら 0 件になる(下で「開き直してください」と断る)
        busy_cols = [col for col in _BUSY_COLUMNS if col in snapshot.columns]
        guard = "".join(
            f" AND COALESCE(TRIM(CAST({_ident(col)} AS TEXT)), '') <> ?" for col in busy_cols
        )
    else:
        busy_cols = []

    params = list(where[1]) + [config.MARK_ON] * len(busy_cols)
    sql = f"DELETE FROM {_ident(table)} WHERE {where[0]}{guard}"
    results, refused = _write(shared, [(sql, params)])
    if refused is not None:
        return refused
    result = results[0]
    if not result.ok:
        applog.error("マスタ削除に失敗: %s", result.error_message)
        return EditResult(False, f"削除できませんでした: {result.error_message}", "failed")
    if result.affected == 0:
        return EditResult(
            False,
            "対象の行が見つからないか、消す直前に発注・発送・注文中になりました。画面を開き直してください。"
            if guard else "対象の行が見つかりませんでした。画面を開き直してください。",
            "not_found",
        )

    applog.info("マスタから削除: %s %s (%s 行)", table, key_text, result.affected)
    return EditResult(True, f"{table} から 1 行消しました。", key=key_text)


def _key_text(value: Any) -> str:
    if isinstance(value, float) and value.is_integer():
        value = int(value)
    return "" if value is None else str(value).strip()


#: 動いている看板かを見る列(発注・発送・注文中。:mod:`kanban.domain.state`)
_BUSY_COLUMNS = state.ACTIVE_COLUMNS


def _busy_state(row: dict[str, Any]) -> str:
    """発注中(赤)・発送済み(緑)・注文中(黄)のどれか。どれでもなければ空。"""
    return "・".join(state.active_labels(row, with_color=True))


def _same_key(a: Any, b: Any) -> bool:
    """キーの値が同じか(DB の ``1`` と画面の ``"1"``、``1.0``、全角)。:mod:`kanban.keys` に任せる。"""
    return keys.same(a, b)


def _where_row(
    snapshot, row_key: Any, expect_key: Any = NO_KEY
) -> tuple[tuple[str, list[Any]] | None, EditResult | None]:
    """1 行だけを指す ``WHERE``。

    **``rowid`` で指します。** 業務のキー(``管理番号``)は重複しうるので、
    そちらで指すと 1 回の操作が 2 行に当たります ── 参照リポで
    「直したつもりが別の行だった」として踏まれた穴です。

    画面が古くて ``rowid`` を持っていないときは、開き直してもらいます
    (当てずっぽうで別の行を触るよりよい)。
    """
    text = "" if row_key is None else str(row_key).strip()
    if not text:
        return None, EditResult(
            False, "どの行かが分かりません。画面を開き直してください。", "no_row_key"
        )
    target = next(
        (r for r in snapshot.rows if str(r.get(ROW_KEY, "")).strip() == text), None
    )
    if target is None:
        return None, EditResult(
            False,
            "対象の行が見つかりませんでした。画面を開き直してください。",
            "not_found",
        )
    # **画面が見ていた行と同じか、キーでも確かめる。** ``rowid`` は、表に
    # INTEGER PRIMARY KEY が無いと振り直されることがある(DB Browser の
    # 「最適化」(VACUUM)、消して入れ直した、など)。画面を開いたあとに振り
    # 直されると、**1 番を直したつもりで 3 番が変わった**(実際に再現した)
    key_column = _key_column(snapshot.columns)
    if expect_key is not NO_KEY and key_column and not _same_key(
        target.get(key_column), expect_key
    ):
        return None, EditResult(
            False,
            "開いたあとに表の中身が入れ替わっています(別の端末が直した・最適化した等)。"
            "画面を開き直してから、もう一度操作してください。",
            "not_found",
        )
    try:
        # 別名(``__行``)ではなく ``rowid`` そのものを書く。別名は
        # UPDATE / DELETE の WHERE では使えない
        return ("rowid = ?", [int(text)]), None
    except ValueError:
        return None, EditResult(
            False, "どの行かが分かりません。画面を開き直してください。", "no_row_key"
        )


def _gate(
    shared: SharedDb | None, table: str, *, configured: bool
) -> tuple[Any, EditResult | None]:
    """書く前に通す関門。通れば ``(いまの中身, None)``。

    順番に意味があります ── **届くか → その表があるか → 読めるか**。
    どれも「断る理由」が違うので、まとめて「できません」にしません。
    """
    blocked, why = availability(shared, configured=configured)
    if blocked:
        return None, EditResult(False, why, blocked)

    assert shared is not None
    view = frame(shared)
    if table not in {t.name for t in view.tables}:
        return None, EditResult(False, f"そのテーブルはありません: {table}", "no_table")

    try:
        # **行を指す値まで読む。** 画面から来た ``__行`` が本当にこの表の
        # 行を指しているかを、書く前に確かめるため
        return shared.read_table(table, with_row_key=True), None
    except SharedDbError as exc:
        return None, EditResult(False, str(exc), BLOCK_UNREADABLE)


def _has_key(rows: list[dict[str, Any]], key_column: str, key_value: Any) -> bool:
    """そのキーの行があるか。

    **管理番号のそろえ方(:mod:`kanban.keys`)で比べます。** 共有 DB の ``管理番号`` には
    数値で入っているもの・文字で入っているもの(``'5.0'`` など)が混ざり得ます。文字の
    完全一致で比べていたころは、画面から来た ``"5"`` と DB の ``'5.0'`` を別物とみなし、
    同じ看板をもう 1 行足せました(中身の入れ替えで表が倍になったのと同じ形)。
    """
    return any(keys.same(r.get(key_column), key_value) for r in rows)


def to_dict(view: MasterView) -> dict[str, Any]:
    data = asdict(view)
    data["page_size"] = PAGE_SIZE
    # 消すときの「いま動いている看板です」の確かめに使う(JS に列名と印を書き写さない)
    data["kanban_state"] = state.for_screen()
    return data
