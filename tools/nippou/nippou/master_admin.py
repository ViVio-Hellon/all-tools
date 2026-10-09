"""マスタ管理 ── 中身を見る / 直す

【なぜ要るのか】
参照するマスタは起動のたびに**黙って読まれるだけ**でした。中に何が
入っているかを見る手立ても、間違いを直す手立ても画面にありません。
値が違っていても、現場には「停止理由が出ない」「直の時刻がずれる」と
いう形でしか現れず、原因に辿り着けません。

しかも相手は **sqlite3** です。Access なら現場のPCで開いて直せましたが、
sqlite3 は**開くための道具が入っていない前提**で考えるほかありません
(テキストエディタでも開けないバイナリ形式です)。確かめる場所と直す
場所を、このツールの中に作ります。

【どこへ書くのか ── 元のファイルへ直に】
読むときは写しを開きます(`source_db`、共有のファイルに触らないため)。
書くときはそうはいきません。写しを書き換えても誰にも届かないので、
**元へ直に書き**、書いたら写しの控えを捨てます ── 捨てないと、
次に読んだときに古い写しが出てきて「直したのに変わらない」になります。

【行をどう指すのか】
sqlite3 の暗黙の `rowid` を使います。マスタには主キーが無い表もあり、
「同じ値の行が2つある」ことも起こりえます。列の値で指すと、そのとき
**両方が書き換わります。** `rowid` なら1行だけを確実に指せます。

【誰が直せるのか ── パスワードを入れた人だけ】
見るのは誰でもできます。**直すのは管理者パスワードを入れたときだけ**に
します ── 参照マスタは他のラインも同じものを見ているので、押し間違いが
全員に届きます。見えることと直せることは別の話です。

【何を直せるのか】
現場が持っている表だけです。上流(ホスト系)が作る仕掛の3ファイルと、
このツール自身が書く日報管理は**見るだけ**にします。理由は
`VIEW_ONLY_WHY` に1つずつ書いてあり、画面にそのまま出ます。
"""
from __future__ import annotations

import sqlite3
import unicodedata
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable, Optional

from . import source_db
from .logging_setup import get_logger
from .presenters import settings as settings_presenter

log = get_logger("master_admin")

# 行を指す隠しの列名。`rowid` をこの名前で持ち回る。
# **業務の列と衝突しない名前**にする(全角を混ぜてあるのはそのため)
ROW_KEY = "__行"

# 一度に出す行数。**全部は出さない。**
# パレットや名簿は数千行あり、全部描いても読めないうえ、共有から引くだけで
# 待たされます。出さなかったぶんは必ず数で言います(`Page.note`)──
# 黙って切ると「これで全部だ」と読めてしまいます。
ROW_LIMIT = 200

# 断りの種類。**文言から推し量らない**
REFUSE_LOCKED = "locked"              # 編集が開いていない(パスワード)
REFUSE_NOT_EDITABLE = "not_editable"  # この表は直す表ではない
REFUSE_BAD_VALUE = "bad_value"        # 入れた値の形が違う
REFUSE_NO_FILE = "no_file"            # そのファイルが無い / 届かない
REFUSE_NO_TABLE = "no_table"          # その表が無い
REFUSE_NO_ROW = "no_row"              # その行がもう無い
REFUSE_CHANGED = "changed"            # 開いたあとに、ほかで直された(v4.24.0)
REFUSE_WRITE_FAILED = "write_failed"  # 書けなかった

# 型の言い方。**画面側で「整数」と書き分けない**(列を足す窓と同じ言葉 ── v4.14.0)
KIND_LABEL = {"int": "整数", "real": "小数", "text": "文字"}


# ==================================================================
# どのファイルの、どの表を直せるか
# ==================================================================
#: 直せるファイル(`presenters/settings.EXPECTED_FILES` の鍵)。
#:
#: **現場が持っている表だけ。** 仕掛の3ファイル(SIKALOT /
#: SIKAHIKI / SIKAODR)は上流のホスト系が作って毎日置き直すので、
#: こちらで直しても次の出力で消えます。日報管理はこのツール自身が
#: 書き戻す先で、直すなら日報入力の画面からです。
EDITABLE_FILES: frozenset[str] = frozenset({"transmission", "material", "vc"})

#: 直せない理由。**出さないのではなく、理由を出す。**
#: 「なぜこのファイルだけ直せないのか」が分からないと、画面が壊れて見える
VIEW_ONLY_WHY: dict[str, str] = {
    "lot": "上流のホスト系が毎日置き直すファイルです。"
           "直しても次の出力で消えます。",
    "hiki": "上流のホスト系が毎日置き直すファイルです。"
            "直しても次の出力で消えます。",
    "order": "上流のホスト系が毎日置き直すファイルです。"
             "直しても次の出力で消えます。",
    "coil": "上流のホスト系が毎日置き直すファイルです。"
            "直しても次の出力で消えます。",
    "access_db": "このツールが書き戻す先です。"
                 "日報を直すなら日報入力の画面から、"
                 "「共有へ保存」で書き直してください。",
}
DEFAULT_VIEW_ONLY = "このツールが直す表ではありません。中身の確認だけできます。"


def view_only_why(file_key: str) -> str:
    """そのファイルを直せない理由。直せるなら空。"""
    if file_key in EDITABLE_FILES:
        return ""
    return VIEW_ONLY_WHY.get(file_key, DEFAULT_VIEW_ONLY)


#: 一覧に出す1文字の目印。**ツール固有なのはここと `EDITABLE_FILES`
#: / `VIEW_ONLY_WHY` / `TABLE_NOTES` だけ**で、あとは表の名前も列も
#: 取り込み元から読みます(列が増減しても画面は直りません)。
#:
#: 目印は名前の代わりではなく、**並んだときの見分け**です ── 一覧は
#: 名前が似た表が縦に並ぶので、形の違いがあると目が止まります。
FILE_MARKS: dict[str, str] = {
    "transmission": "伝",
    "material": "資",
    "lot": "仕",
    "hiki": "引",
    "order": "受",
    "coil": "割",
    "access_db": "報",
    "vc": "巻",
}

# ==================================================================
# 表ごとの決まり ── **ファイル単位では足りない表だけ**
#
# VC計算マスタ(`nippou/vc`)は vc-calculator の表をそのまま使います。
# あちらのマスタ管理が表ごとに持っていた決まりを、ここに写しました。
# ==================================================================
#: 一覧に出さない表。**画面に出さない内部の値**(版・更新番号。vc-calculator
#: が作ったものには管理者パスワードの撹拌値も入っている)
HIDDEN_TABLES: frozenset[tuple[str, str]] = frozenset({("vc", "_メタ")})

#: 見るだけの表と、その理由
VIEW_ONLY_TABLES: dict[tuple[str, str], str] = {
    ("vc", "変更履歴"): "マスタを直すたびに自動で増える記録です。直せません。",
}

#: 行を足す・消すができない表(値だけ直す)。**キーは読む側が名前で引く**ので、
#: 足しても読まれず、消すと既定に戻らない
FIXED_ROWS: dict[tuple[str, str], str] = {
    ("vc", "アプリ設定"): "アプリ設定は決まったキーの行だけです。値を直してください"
                        "(足す・消すはできません)。",
}


def is_hidden(file_key: str, table: str) -> bool:
    """一覧に出さない表か。**足した列の記録**(`ADDED_REGISTRY`)はどのファイルでも出さない。"""
    return (file_key, table) in HIDDEN_TABLES or table == ADDED_REGISTRY


#: 表ごとの短い説明。**何の表かが名前から読めないものだけ**書く。
TABLE_NOTES: dict[str, str] = {
    # 停止内訳.csv に同じ分類の行があれば、**この表は使われません**
    # (`logic/stop_csv`)。直した人が効いたつもりで帰らないように言う
    "作業停止時間内訳_1": "停止理由(管理ロス)。記号は 0〜6 の数字。"
                          "停止内訳.csv に 1 の行があればそちらが勝ちます",
    "作業停止時間内訳_2": "停止理由(突発・待ち)。記号は イロハ… の片仮名。"
                          "停止内訳.csv に 2 の行があればそちらが勝ちます",
    "作業停止時間内訳_3": "停止理由(ハンドリング)。記号は A〜G の英字。"
                          "停止内訳.csv に 3 の行があればそちらが勝ちます",
    "時間用": "直の境界時刻。直は 1・2・3・昼、開始と終了は 07:00 の形。"
              "直すと直の時間にすぐ効きます",
    "注意_包装仕様": "包装仕様NOごとの注意。フラグ(用途コード/寸法/納入先)で"
                     "出す条件が決まる。コイルは画面に出す",
    "資材重量": "資材ごとの単位質量と係数(GW計算)",
    "VC重量": "VCフィルムの品名ごとの単位質量",
    "班員名簿": "作業者の名前と班(作業者を選ぶ)",
    # **ここだけ「勝ち負けがある」と書いてあります。**
    #
    # 45度線の目標は、この表と ライン毎目標.csv の**両方**から読みます。
    # 同じラインが両方にあれば CSV が勝ちます(`logic/line_target.merge`)。
    # ここで言っておかないと、この表を直した人は効いたつもりで帰ります
    # ── CSV に同じラインが書いてあれば、直した値は出ません。
    #
    # 飾り(**…**)は使いません ── この文は選択欄の項目名として
    # **そのまま出る**ので、書いた記号がそのまま見えます
    # (`static/js/views/settings.js`)。
    "ライン毎目標": "ラインごとの目標枚数(45度線)。こことCSVの両方から"
                    "読み、同じラインなら ライン毎目標.csv が勝ちます",
    # ---- VC計算マスタ(VC長さ計算) ----
    "VC品種": "VC長さ計算の品種一覧と、選んだときに入る VC厚・内径。"
              "内径選択肢がある品種は内径を空に(大/小から選ぶ)",
    "VC内径選択肢": "内径を選ぶ品種の選択肢(大 95.0 / 小 87.0 など)",
    "枚数定尺": "枚数を出す製品の丈(1×2 = 2010mm など)",
    "早見表ブロック": "早見表の枠。計算品種の VC厚 で長さを式から出す(空なら固定値)。"
                      "足す・消す・式/固定値の切り替えは VC長さ計算 → 設定 が1回で済む",
    "早見表値": "早見表のマス(内径 × 肉厚)。長さは計算品種の無い枠だけ使う。"
                "行・列ごと消すのは VC長さ計算 → 設定 で",
    "アプリ設定": "早見表の見出し・丸め・肉厚の逆算(キーは決まっている)",
    "変更履歴": "VC計算マスタを直すたびに自動で1行増える記録",
}


# ==================================================================
# 直せるかどうか
# ==================================================================
def can_edit(file_key: str, *, unlocked: bool,
             table: str = "") -> tuple[bool, str]:
    """このファイルを直せるか。**直せないなら理由も返す。**

    関門は2つで、**順番に意味があります** ── 表 → パスワード。
    先にパスワードを言うと、そもそも直せない表に対して
    「パスワードを入れれば直せる」と読ませてしまいます。
    """
    why = view_only_why(file_key) or VIEW_ONLY_TABLES.get((file_key, table), "")
    if why:
        return False, why
    if not unlocked:
        # **どこで開けるのかまで言う。** 「要ります」だけだと、押す場所を
        # 画面の中から探させることになる(鍵は面のいちばん上の帯1つ)
        return False, ("直すには管理者パスワードが要ります。"
                       "この面のいちばん上にある「鍵を開ける」から入れてください。")
    return True, ""


# ==================================================================
# 見る
# ==================================================================
@dataclass
class FileInfo:
    """一覧に出すファイル1つ。"""

    key: str
    label: str
    path: str
    kind: str
    readable: bool
    editable: bool
    why: str = ""
    tables: list[str] = field(default_factory=list)
    error: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {"key": self.key, "label": self.label, "path": self.path,
                "kind": self.kind, "readable": self.readable,
                "editable": self.editable, "why": self.why,
                "tables": self.tables, "error": self.error}


def files() -> list[FileInfo]:
    """このツールが読むファイルぜんぶ。**直せないものも出す。**

    直せるものだけを出すと「あるはずのファイルが無い」に見えます。
    中身を確かめるのは全部でできるので、並べたうえで直せるかどうかを
    札にします。
    """
    out: list[FileInfo] = []
    for view in settings_presenter.file_views():
        out.append(FileInfo(
            key=view.key, label=view.label, path=str(view.path),
            kind=view.kind, readable=view.readable,
            editable=view.key in EDITABLE_FILES,
            why=view_only_why(view.key),
            tables=[t for t in view.tables if not is_hidden(view.key, t)],
            error=view.error))
    return out


def _file(file_key: str) -> Optional[Any]:
    return next((v for v in settings_presenter.file_views()
                 if v.key == file_key), None)


@dataclass
class CatalogGroup:
    """一覧に出すファイル1つと、その中の表。"""

    file: str
    label: str
    mark: str
    path: str = ""
    kind: str = ""
    readable: bool = False
    editable: bool = False
    why: str = ""
    error: str = ""
    tables: list[dict[str, str]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {"file": self.file, "label": self.label, "mark": self.mark,
                "path": self.path, "kind": self.kind,
                "readable": self.readable, "editable": self.editable,
                "why": self.why, "error": self.error, "tables": self.tables}


def catalog(infos: Optional[list[FileInfo]] = None) -> list[CatalogGroup]:
    """一覧に出すもの全部。**直せる側を先に、直せないものも出す。**

    選択欄を2つ並べていたときは、開くまで何があるか分かりませんでした。
    一覧にしておくと、**選ぶ前に**どの表が直せてどれが見るだけかが
    読めます ── 直せないものを隠すと「あるはずの表が無い」に見えます。
    """
    groups = [CatalogGroup(
        file=info.key, label=info.label, mark=FILE_MARKS.get(info.key, "表"),
        path=info.path, kind=info.kind, readable=info.readable,
        editable=info.editable, why=info.why, error=info.error,
        tables=table_notes(info.tables))
        for info in (infos if infos is not None else files())]
    # 直せるものが上。**いちばん触るものを探させない**
    groups.sort(key=lambda g: (not g.editable, not g.readable))
    return groups


@dataclass
class Column:
    """打ち込める列1つ。"""

    name: str
    kind: str = "text"
    required: bool = False
    #: マスタ管理の「列を足す」で足した列(v4.14.0)。計算・帳票には使われない
    added: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {"name": self.name, "kind": self.kind,
                "kind_label": KIND_LABEL.get(self.kind, self.kind),
                "required": self.required, "added": self.added,
                "note": ADDED_COLUMN_NOTE if self.added else ""}


def _kind_of(declared: str) -> str:
    upper = (declared or "").upper()
    if "INT" in upper:
        return "int"
    if "REAL" in upper or "FLOA" in upper or "DOUB" in upper:
        return "real"
    return "text"


def columns(path: Path, table: str) -> list[Column]:
    """その表の、打ち込める列。

    **本当にある列だけ**を出します(`PRAGMA table_info`)。無い列を出すと、
    打ち込めてしまい、保存の瞬間に「そんな列は無い」と断られます。

    `rowid` の別名になっている INTEGER PRIMARY KEY の列は外します ──
    行を指す番号そのものなので、打ち替えると別の行になります。
    """
    try:
        with source_db.open_source(Path(path)) as conn:
            rows = list(conn.execute(
                f"PRAGMA table_info({source_db.quote_identifier(table)})"))
    except source_db.SourceError as exc:
        log.warning("%s の列を引けません: %s", table, exc)
        return []

    added = set(added_columns(Path(path), table))
    out: list[Column] = []
    for row in rows:
        kind = _kind_of(str(row["type"]))
        if row["pk"] and kind == "int":
            # INTEGER PRIMARY KEY は rowid の別名。触らせない
            continue
        out.append(Column(
            name=row["name"], kind=kind,
            # 既定値のある列は空欄で通してよい。NULL も既定も無い列だけが必須
            required=bool(row["notnull"]) and row["dflt_value"] is None,
            added=row["name"] in added))
    return out


@dataclass
class Page:
    """1つの表の中身(の一部)。"""

    file: str = ""
    table: str = ""
    columns: list[str] = field(default_factory=list)
    rows: list[dict[str, Any]] = field(default_factory=list)
    total: int = 0
    editable: bool = False
    why: str = ""
    note: str = ""
    error: str = ""
    sort: str = ""
    sort_dir: str = "asc"
    #: 読み書きするファイルそのもの。**どこを直しているのかを画面に出す**
    #: ── 手元の写しではなく元のファイルなので、他の端末にも効く
    source: str = ""
    #: 行を足す・消すができるか(`FIXED_ROWS` の表は値を直すだけ)
    can_add: bool = False
    #: 表ぜんぶの点検で気になった行(アクセス権限 v4.12.3・班員名簿のオペレーター v4.15.0)
    checks: list[str] = field(default_factory=list)
    #: 点検の見出し(表ごとに、読めないと何に効かないかが違う)
    checks_head: str = ""
    #: 列を足せるか(v4.14.0)。**直せる表で、鍵が開いているときだけ**
    can_add_column: bool = False
    #: 足せない理由(直せる表なのに足せないとき。VC計算マスタ)
    add_column_why: str = ""
    #: 足す前に言っておくこと(足した列が何に効くか)
    add_column_note: str = ""
    #: この表にマスタ管理で足した列
    added_columns: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {"file": self.file, "table": self.table,
                "columns": self.columns, "rows": self.rows,
                "total": self.total, "shown": len(self.rows),
                "editable": self.editable, "why": self.why,
                "can_add": self.can_add, "can_delete": self.can_add,
                "can_add_column": self.can_add_column,
                "add_column_why": self.add_column_why,
                "add_column_note": self.add_column_note,
                "added_columns": self.added_columns,
                "note": self.note, "error": self.error, "checks": self.checks,
                "checks_head": self.checks_head,
                "row_key": ROW_KEY, "source": self.source,
                "sort": self.sort, "sort_dir": self.sort_dir}


def page(file_key: str, table: str, *, query: str = "", sort: str = "",
         sort_dir: str = "asc", unlocked: bool = False,
         limit: int = ROW_LIMIT) -> Page:
    """表の中身を読む。

    `sort` は列名(見出しを押したとき)。**本当にある列だけ**を許します
    ── 列名をそのまま `ORDER BY` に組み込むので、絞り込みと同じく
    許可リストで確かめてから使います。無効な指定は黙って既定(`rowid`)へ
    戻します(断ると、押しただけで断られる画面になる)。
    """
    allowed, why = can_edit(file_key, unlocked=unlocked)
    view = Page(file=file_key, table=table, editable=allowed, why=why)

    target = _file(file_key)
    if target is None:
        view.error = "そのファイルはありません。"
        return view
    if target.kind != "sqlite3":
        view.error = ("このファイルは Access なので、中身を出せません。"
                      "sqlite3 に切り替えると見られます。")
        return view
    view.source = target.path
    if not target.readable:
        view.error = target.error or "読めません。"
        return view
    shown = [t for t in target.tables if not is_hidden(file_key, t)]
    if table and table not in shown:
        view.error = f"{table} という表はありません。"
        return view
    view.table = table or (shown[0] if shown else "")
    if not view.table:
        view.error = "このファイルには表がありません。"
        return view
    # **表ごとの決まり**(見るだけの表・足せない表)はここで分かる
    view.editable, view.why = can_edit(file_key, unlocked=unlocked, table=view.table)
    view.can_add = view.editable and (file_key, view.table) not in FIXED_ROWS
    if view.editable and not view.can_add:
        view.why = FIXED_ROWS[(file_key, view.table)]

    path = Path(target.path)
    names = source_db.columns(path, view.table)
    if not names:
        view.error = f"{view.table} の列を読めません。"
        return view
    view.columns = names

    where, params = _filter(names, query)
    order, sort_col = _order(names, sort, sort_dir)
    view.sort = sort_col
    view.sort_dir = "desc" if sort_dir == "desc" else "asc"
    quoted = source_db.quote_identifier(view.table)
    try:
        counted = source_db.read_query(
            path, f"SELECT COUNT(*) AS n FROM {quoted}{where}", params)
        view.total = int(counted[0]["n"]) if counted else 0
        rows = source_db.read_query(
            path,
            f'SELECT rowid AS "{ROW_KEY}", * FROM {quoted}{where}'
            f" {order} LIMIT ?", [*params, max(1, int(limit))])
    except source_db.SourceError as exc:
        view.error = str(exc)
        return view

    view.rows = [{k: "" if v is None else str(v) for k, v in row.items()}
                 for row in rows]
    view.added_columns = [n for n in added_columns(path, view.table) if n in names]
    if view.editable:
        view.add_column_why = add_column_why(file_key, view.table)
        view.can_add_column = not view.add_column_why
        view.add_column_note = ADD_COLUMN_NOTE if view.can_add_column else ""
    if is_access_table(view.table):
        view.checks = _access_checks(path, view.table)
        if view.checks:
            view.checks_head = (f"読めない行が{len(view.checks)}行あります"
                                "(このままでは、そのPCのラインに効きません)")
    elif is_staff_table(file_key, view.table):
        view.checks = _staff_checks(path, view.table)
        if view.checks:
            view.checks_head = (f"オペレーターが読めない人が{len(view.checks)}人います"
                                "(標準作業時間・梱包力では「区分なし」として数えます)")
    hidden = view.total - len(view.rows)
    if hidden > 0:
        # **黙って切らない。** 絞り込みの手があることまで言う。並び替えは
        # 出している分だけでなく**全件**でしている(上から何件かを出す)
        head = "並び替えた上から " if view.sort else ""
        view.note = (f"{view.total}件のうち {head}{len(view.rows)}件を出しています"
                     f"(ほか {hidden}件)。絞り込むと目当ての行が出ます。")
    return view


def _order(names: list[str], sort: str, sort_dir: str) -> tuple[str, str]:
    """見出しを押したときの並び替え。

    `sort` が実在の列でなければ、押していないのと同じ(`rowid` の既定順)
    へ静かに戻します ── マスタの列は上流の都合で増減するので、もう無い
    列を指した並び替えを断ると「さっきまで押せたのに」が起きます。
    """
    if sort and sort in names:
        direction = "DESC" if sort_dir == "desc" else "ASC"
        col = source_db.quote_identifier(sort)
        text = f"trim({col})"
        # 【数は数として並べる】(v3.98.0)
        # Access から移したマスタは、数も**文字で**入っていることが多い。
        # 文字のまま並べると 1, 10, 100, 2 … になり、押しても並んで見えない。
        # 数字(と . + -)だけの文字は数として並べ、空は向きに関係なく最後へ
        numeric = (f"(typeof({col}) IN ('integer', 'real') OR "
                   f"({text} GLOB '*[0-9]*' AND {text} NOT GLOB '*[^0-9.+-]*'))")
        return (f"ORDER BY CASE WHEN {col} IS NULL OR {text} = '' THEN 2"
                f" WHEN {numeric} THEN 0 ELSE 1 END,"
                f" CASE WHEN {numeric} THEN CAST({text} AS REAL) END {direction},"
                # 同値が並ぶと表示順が揺れるので、rowid で確定させる
                f" {col} {direction}, rowid ASC", sort)
    return "ORDER BY rowid ASC", ""


def _filter(names: list[str], query: str) -> tuple[str, list[Any]]:
    """絞り込みの条件。**どの列でもいい**ので、全部の列を見ます。

    どの列に何が入っているかを覚えていなくても引けるようにします。
    """
    text = (query or "").strip()
    if not text:
        return "", []
    conds = " OR ".join(
        f"CAST({source_db.quote_identifier(n)} AS TEXT) LIKE ?" for n in names)
    return f" WHERE ({conds})", [f"%{text}%"] * len(names)


# ==================================================================
# 直す
# ==================================================================
@dataclass
class Result:
    ok: bool = True
    message: str = ""
    reason: str = ""
    #: 手元へ写し直した直の時刻 `[[直, 開始, 終了], …]`。写し直したときだけ。
    #: **同じ画面の「直の境界時刻」の表を描き直すため**(描き直さないと、
    #: 直した直後にその面を開いた人が古い時刻を見る)
    shift_times: Optional[list[list[str]]] = None


def _as_shown(value: Any) -> str:
    """一覧に出したときの形(`page` と同じ ── None は空、ほかは文字)。"""
    return "" if value is None else str(value)


def changed_since(before: Optional[dict[str, Any]],
                  original: Optional[dict[str, Any]]) -> list[str]:
    """**開いたときの値**(`original`)から、いまの行で変わっている列。

    比べるのは `original` に入っている列だけです ── 画面は「直した列」の
    開いたときの値だけを送ってくるので、**別の列をほかの人が直したぶんは
    ぶつかりません**(そのまま残ります)。同じ列をほかの人が先に直していたら、
    ここに挙がります。
    """
    if not original or before is None:
        return []
    return [name for name, seen in original.items()
            if name in before and _as_shown(before[name]) != _as_shown(seen)]


def save_row(file_key: str, table: str, row_key: Any,
             values: dict[str, Any], *, unlocked: bool = False,
             original: Optional[dict[str, Any]] = None) -> Result:
    """1行を書き換える。

    `original` は**画面が行を開いたときの値**(直した列のぶん。v4.24.0)。

    【2つの端末で同じ行を直すと、先に直したほうが消えていました】
    画面は窓を開いたときの1行ぶんを**まるごと**送っていたので、あいだに
    ほかの端末が別の列を直していても、開いたときの古い値で全部の列を
    書き戻していました(打った値が勝手に戻ることがありました)。

    画面は直した列だけを送り、その列の開いたときの値を添えます。書く直前の
    行と比べて、**同じ列がほかで直されていれば書かずに断ります**(409)。
    添えてこない古い画面は、これまでどおり書きます。
    """
    path, refused = _ready(file_key, table, unlocked)
    if refused:
        return refused

    clean, problem = _clean(path, table, values, filling=False)
    if problem:
        return Result(False, problem, REFUSE_BAD_VALUE)
    if not clean:
        return Result(False, "変える値がありません。", REFUSE_BAD_VALUE)

    before = None
    try:
        _vc_backup(file_key, path)
        with source_db.connect(path, foreign_keys=file_key in FK_FILES) as src:
            # 表ごとの決まりは、**書く直前に、書く相手で**確かめる
            # (同じ直が2行あるか、は元のファイルを見ないと分からない)
            problem = _table_problem(src, file_key, table, clean,
                                     row_key=_row_key(row_key))
            if problem:
                return Result(False, problem, REFUSE_BAD_VALUE)
            before = _row_before(src, file_key, table, _row_key(row_key))
            if original:
                # **書く直前の行で比べる**(書き込みのロックの中。一覧を出した
                # ときの値ではなく、いまの元のファイルの値)
                now = _row_now(src, table, _row_key(row_key))
                if now is None:
                    return Result(False, "その行はもうありません。一覧を出し直してください。",
                                  REFUSE_NO_ROW)
                moved = changed_since(now, original)
                if moved:
                    log.info("マスタの行は開いたあとに直されていました: %s / %s rowid=%s %s",
                             file_key, table, row_key, moved)
                    return Result(
                        False,
                        f"この行の {'・'.join(moved)} は、開いたあとにほかで直されています。"
                        "上書きしないよう、書きませんでした。一覧を出し直して、"
                        "いまの値を見てから直してください。",
                        REFUSE_CHANGED)
            changed = src.update(table, clean, {"rowid": _row_key(row_key)})
    except source_db.SourceError as exc:
        return _write_failed(table, exc, file_key, "更新")
    if not changed:
        # 一覧を出したあとに誰かが消した。押した人には見えていない事実
        return Result(False, "その行はもうありません。一覧を出し直してください。",
                      REFUSE_NO_ROW)

    log.info("マスタを直しました: %s / %s rowid=%s %s",
             file_key, table, row_key, sorted(clean))
    _vc_note(file_key, path, table, "更新", _row_key(row_key), before, clean)
    return Result(True, f"{table} の1行を直しました。")


def add_row(file_key: str, table: str, values: dict[str, Any], *,
            unlocked: bool = False) -> Result:
    """1行足す。"""
    path, refused = _ready(file_key, table, unlocked, adding_or_deleting=True)
    if refused:
        return refused

    clean, problem = _clean(path, table, values, filling=True)
    if problem:
        return Result(False, problem, REFUSE_BAD_VALUE)
    # **全部空なら足さない。** 足す行は全部の列を持つので(`filling`)、
    # 空で押しても `clean` は空になりません ── 以前はそのまま中身の無い
    # 行が1つ増え、共有のマスタに残っていました
    if all(value is None for value in clean.values()):
        return Result(False, "入れる値がありません。", REFUSE_BAD_VALUE)

    row = None
    try:
        _vc_backup(file_key, path)
        with source_db.connect(path, foreign_keys=file_key in FK_FILES) as src:
            problem = _table_problem(src, file_key, table, clean, row_key=None)
            if problem:
                return Result(False, problem, REFUSE_BAD_VALUE)
            src.insert(table, clean)
            found = src.query("SELECT last_insert_rowid() AS r")
            row = int(found[0]["r"]) if found else None
    except source_db.SourceError as exc:
        return _write_failed(table, exc, file_key, "追加")

    log.info("マスタに足しました: %s / %s %s", file_key, table, sorted(clean))
    _vc_note(file_key, path, table, "追加", row, None, clean)
    return Result(True, f"{table} に1行足しました。")


def delete_row(file_key: str, table: str, row_key: Any, *,
               unlocked: bool = False) -> Result:
    """1行消す。"""
    path, refused = _ready(file_key, table, unlocked, adding_or_deleting=True)
    if refused:
        return refused

    before = None
    try:
        _vc_backup(file_key, path)
        with source_db.connect(path, foreign_keys=file_key in FK_FILES) as src:
            before = _row_before(src, file_key, table, _row_key(row_key))
            changed = src.delete(table, {"rowid": _row_key(row_key)})
    except source_db.SourceError as exc:
        return _write_failed(table, exc, file_key, "削除")
    if not changed:
        return Result(False, "その行はもうありません。一覧を出し直してください。",
                      REFUSE_NO_ROW)

    log.info("マスタから消しました: %s / %s rowid=%s", file_key, table, row_key)
    _vc_note(file_key, path, table, "削除", _row_key(row_key), before, None)
    return Result(True, f"{table} の1行を消しました。")


def _row_key(value: Any) -> int:
    """`rowid` は整数。数字でないものは行を指せない。"""
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return -1


def _ready(file_key: str, table: str, unlocked: bool, *,
           adding_or_deleting: bool = False,
           ) -> tuple[Optional[Path], Optional[Result]]:
    """書く前に通す関門。通れば `(ファイル, None)`、通らなければ理由。

    順番に意味があります ── **表 → パスワード → 届くか → 表が本当に
    あるか。** 届かないことを先に言うと、直せない表に対して
    「共有が落ちている」と読ませてしまいます。
    """
    if is_hidden(file_key, table):
        return None, Result(False, f"{table} という表はありません。", REFUSE_NO_TABLE)
    allowed, why = can_edit(file_key, unlocked=unlocked, table=table)
    if not allowed:
        reason = (REFUSE_LOCKED if file_key in EDITABLE_FILES
                  and (file_key, table) not in VIEW_ONLY_TABLES
                  else REFUSE_NOT_EDITABLE)
        return None, Result(False, why, reason)
    if adding_or_deleting and (file_key, table) in FIXED_ROWS:
        return None, Result(False, FIXED_ROWS[(file_key, table)], REFUSE_NOT_EDITABLE)

    target = _file(file_key)
    if target is None:
        return None, Result(False, "そのファイルはありません。", REFUSE_NO_FILE)
    if target.kind != "sqlite3":
        return None, Result(
            False, "このファイルは Access なので、ここからは直せません。",
            REFUSE_NOT_EDITABLE)
    if not target.readable:
        return None, Result(
            False, target.error or f"{target.path} に届きません。",
            REFUSE_NO_FILE)
    if table not in target.tables:
        return None, Result(False, f"{table} という表はありません。",
                            REFUSE_NO_TABLE)
    return Path(target.path), None


def _write_failed(table: str, exc: Exception, file_key: str = "",
                  operation: str = "") -> Result:
    log.warning("%s へ書けませんでした: %s", table, exc)
    if file_key in FK_FILES:
        # 表の決まりに当たった。**生の英文を見せない**(何を直せばよいかを言う)
        from .vc import db as vc_db
        return Result(False, vc_db.integrity_message(operation, str(exc), table),
                      REFUSE_BAD_VALUE)
    return Result(False, f"書けませんでした: {exc}", REFUSE_WRITE_FAILED)


# ==================================================================
# VC計算マスタ ── 書く前の控え・書いたあとの変更履歴と更新番号
# ==================================================================
#: 表どうしの結びつき(外部キー)を決めてあるファイル。書くときに守らせる
FK_FILES: frozenset[str] = frozenset({"vc"})


def _row_now(src: source_db.SourceConnection, table: str,
             row: int) -> Optional[dict[str, Any]]:
    """書く直前の1行(どのファイルでも)。無ければ None。"""
    found = src.query(f"SELECT * FROM {source_db.quote_identifier(table)}"
                      " WHERE rowid = ?", [row])
    return found[0] if found else None


def _row_before(src: source_db.SourceConnection, file_key: str, table: str,
                row: int) -> Optional[dict[str, Any]]:
    """変更履歴に残す「変更前」。VC計算マスタのときだけ読む。"""
    if file_key != "vc":
        return None
    found = src.query(f"SELECT * FROM {source_db.quote_identifier(table)}"
                      " WHERE rowid = ?", [row])
    return found[0] if found else None


def _vc_backup(file_key: str, path: Path) -> None:
    """直した日の最初に、元のファイルの控えを取る(vc-calculator と同じ `backup/`)。"""
    if file_key != "vc":
        return
    from .vc import db as vc_db
    vc_db.backup_daily(path)


def _vc_note(file_key: str, path: Path, table: str, operation: str,
             row: Optional[int], before: Optional[dict[str, Any]],
             after: Optional[dict[str, Any]]) -> None:
    """VC計算マスタを書いたあと。**変更履歴に残し、更新番号を上げる。**

    残せなくても、書いたことは取り消しません(元のファイルにはもう書けて
    います)。ログに残し、計算画面には次の要求で効かせます。
    """
    if file_key != "vc":
        return
    from .vc import db as vc_db
    from .vc import masters as vc_masters
    try:
        vc_db.note_edit(path, table, operation, row, before, after)
    except Exception:                                # noqa: BLE001 - 書いたことは残す
        log.exception("VC計算マスタの変更履歴を残せませんでした")
    vc_masters.invalidate()


def _clean(path: Path, table: str, values: dict[str, Any], *,
           filling: bool) -> tuple[dict[str, Any], str]:
    """画面から来た値を、書ける形にする。

    `filling` が真なら新しい行なので、送られてこなかった必須の列も見ます。
    偽なら書き換えなので、**送られてきた列だけ**を触ります(送っていない
    列を消さないため)。
    """
    out: dict[str, Any] = {}
    for column in columns(path, table):
        if column.name not in values and not filling:
            continue
        raw = str(values.get(column.name, "")).strip()
        if raw == "":
            if column.required:
                return {}, f"「{column.name}」は空にできません。"
            # 空欄は「無し」。読むときは空文字として出る
            out[column.name] = None
            continue
        if column.kind == "int":
            try:
                out[column.name] = int(float(raw))
            except ValueError:
                return {}, f"「{column.name}」は{KIND_LABEL['int']}で入れてください。"
        elif column.kind == "real":
            try:
                out[column.name] = float(raw)
            except ValueError:
                return {}, f"「{column.name}」は{KIND_LABEL['real']}で入れてください。"
        else:
            out[column.name] = raw
    return out, ""


# ==================================================================
# 列を足す(v4.14.0 ── python-web-tools VER4.2.0 と同じ作り)
# ==================================================================
#     マスタ管理で表を選び、「+ 列を足す…」を押します。
#     入れるもの: 列の名前、型(文字 / 整数 / 小数)、最初の値
#     列を足せるのは、マスタ管理で直せる表だけ
#
# 【足した列にできること】
# マスタ管理で見る・直すだけ。日報の計算・帳票は、どの列を読むかがプログラムに
# 決まっているので、足した列は使いません(使うにはプログラムを直す)。
#
# 【戻せない】
# 列を消す口は作りません(ほかの道具・ほかの端末が同じ表を読んでいる)。なので
# 鍵(管理者パスワード)を通し、型と最初の値まで決めてから、確認を1回出して足します。
#
# 【1回で確定】
# 列を足す・最初の値を入れる・記録する、を1つのトランザクションで(`SourceConnection.
# transaction`)。途中で落ちたら列も足されない ── 半分だけ足された表を共有に残さない。
ADD_KINDS: dict[str, str] = {"text": "TEXT", "int": "INTEGER", "real": "REAL"}
#: 列の名前の長さの上限(41文字以上は断る)
COLUMN_NAME_LIMIT = 40
#: 名前に使わせない文字。列名は `"..."` で囲んで文へ組み込むので、囲みの記号が混ざると
#: 文が壊れる。`.` は Access へ戻すときに断られる。改行などの制御文字も
_BAD_NAME_CHARS = frozenset('[]"`\'.') | frozenset(chr(c) for c in range(32)) | {chr(127)}
#: sqlite3 が行番号として扱う名前。列にすると rowid の代わりに読まれてしまう
_RESERVED_NAMES = frozenset({"rowid", "oid", "_rowid_"})

#: 足した列の記録。**足した表と同じファイルの中に置く**(どの端末からでも「足した列」と
#: 分かるように)。マスタ管理の一覧には出さない(`is_hidden`)
ADDED_REGISTRY = "ツールで足した列"
ADDED_REGISTRY_COLUMNS: tuple[str, ...] = ("表", "列", "型", "最初の値", "足した端末", "足した日時")

#: 足せないファイルと、その理由。**直せる表なのに足せない**ものだけ
NO_ADD_COLUMN_FILES: dict[str, str] = {
    # VC長さ計算の設定画面(と vc-calculator)が、列を決めて行を書く。足した列を
    # 知らないので、そちらで足した行の値は空のまま ── 足しても誰も埋めない
    "vc": "VC計算マスタの表は VC長さ計算の画面も行を書く(足した列を知らない)ので、"
          "列は足せません。",
}

#: 足す前に言うこと(窓の頭に出す)
ADD_COLUMN_NOTE = ("足した列はマスタ管理で見る・直すだけです。日報の計算・帳票には使われません"
                   "(使うにはプログラムを直します)。共有の元ファイルに足すので、ほかの端末の"
                   "マスタ管理にも出ます。列は消せません。")
#: 足した列に添える一言(行を開いたとき・一覧の下)
ADDED_COLUMN_NOTE = "マスタ管理で足した列(計算・帳票には使われません)"


def added_columns(path: Optional[Path], table: str) -> dict[str, str]:
    """「列を足す」で足した列 {列名: 型("int"/"real"/"text")}。足した順。"""
    if path is None or not table:
        return {}
    try:
        rows = source_db.read_query(
            Path(path),
            f'SELECT "列", "型" FROM {source_db.quote_identifier(ADDED_REGISTRY)}'
            ' WHERE "表" = ? ORDER BY rowid', [table])
    except source_db.SourceError:
        return {}                                 # まだ1つも足していない
    return {str(r["列"]): str(r.get("型") or "text") for r in rows if r.get("列")}


def add_column_why(file_key: str, table: str) -> str:
    """その表に列を足せない理由。足せるなら空(鍵・表があるかは `_ready` が見る)。"""
    why = view_only_why(file_key) or VIEW_ONLY_TABLES.get((file_key, table), "")
    if why:
        return f"マスタ管理で直す表にだけ列を足せます。{why}"
    return NO_ADD_COLUMN_FILES.get(file_key, "")


def column_name_why(name: str, have: Iterable[str]) -> str:
    """列の名前を断る理由。使えるなら空。"""
    if not name:
        return "列の名前を入れてください。"
    if len(name) > COLUMN_NAME_LIMIT:
        return f"列の名前は{COLUMN_NAME_LIMIT}文字までにしてください({len(name)}文字あります)。"
    bad = sorted({c for c in name if c in _BAD_NAME_CHARS})
    if bad:
        shown = " ".join(c if c.isprintable() else "(改行など)" for c in bad)
        return f"列の名前に {shown} は使えません。"
    if name.lower() in _RESERVED_NAMES or name.startswith("__"):
        return f"「{name}」はこのツールが中で使う名前なので使えません。"
    # sqlite3 の列名は英字の大文字・小文字を区別しない。区別して比べると、「Code」が
    # あるのに「code」を足そうとして、書く瞬間に断られる
    for column in have:
        if str(column).lower() == name.lower():
            return f"「{column}」という列がもうあります。"
    return ""


def initial_value(kind: str, raw: str) -> tuple[Any, str]:
    """最初の値を、その型の値にする。`(値, 断る理由)`。空なら `(None, "")`(空のまま)。"""
    text = str(raw or "").strip()
    if not text:
        return None, ""
    if kind == "text":
        return text, ""
    folded = unicodedata.normalize("NFKC", text).replace(",", "")
    try:
        number = float(folded)
    except ValueError:
        number = None
    if number is None or number != number or number in (float("inf"), float("-inf")):
        return None, f"最初の値は{KIND_LABEL[kind]}で入れてください。"
    if kind == "int":
        if not number.is_integer():
            return None, "最初の値は整数で入れてください(小数は入れられません)。"
        return int(number), ""
    return number, ""


def _terminal() -> str:
    """足した端末の名前(記録に残す)。"""
    try:
        from .services import access_rights

        return access_rights.identity().pc
    except Exception:                             # noqa: BLE001 - 名前が分からないだけ
        return ""


def add_column(file_key: str, table: str, name: Any, kind: Any = "text",
               initial: Any = "", *, unlocked: bool = False) -> Result:
    """直せる表に列を1つ足す。`initial` はいまある行すべてに入れる最初の値(空なら空のまま)。

    関門の順は `_ready` と同じ考え ── **表 → 鍵 → 届くか → 表があるか**。直せない表に
    「鍵を開ければ足せる」と読ませない。
    """
    why = add_column_why(file_key, table)
    if why:
        return Result(False, why, REFUSE_NOT_EDITABLE)
    path, refused = _ready(file_key, table, unlocked)
    if refused:
        return refused

    name = str(name or "").strip()
    kind = str(kind or "text")
    if kind not in ADD_KINDS:
        return Result(False, "列の型は 文字 / 整数 / 小数 から選んでください。", REFUSE_BAD_VALUE)
    problem = column_name_why(name, source_db.columns(path, table))
    if problem:
        return Result(False, problem, REFUSE_BAD_VALUE)
    value, problem = initial_value(kind, initial)
    if problem:
        return Result(False, problem, REFUSE_BAD_VALUE)

    q = source_db.quote_identifier
    registry = q(ADDED_REGISTRY)
    filled = 0
    try:
        with source_db.connect(path) as src:
            with src.transaction() as tx:
                # **書く相手で確かめ直す**(確かめたあとに、ほかの端末が同じ名前を足した)
                have = [r["name"] for r in tx.query(f"PRAGMA table_info({q(table)})")]
                problem = column_name_why(name, have)
                if problem:
                    return Result(False, problem, REFUSE_BAD_VALUE)
                tx.execute(f"ALTER TABLE {q(table)} ADD COLUMN {q(name)} {ADD_KINDS[kind]}")
                if value is not None:
                    filled = tx.execute(f"UPDATE {q(table)} SET {q(name)} = ?", [value])
                cols = ", ".join(q(c) for c in ADDED_REGISTRY_COLUMNS)
                tx.execute(f"CREATE TABLE IF NOT EXISTS {registry} ({cols},"
                           ' PRIMARY KEY ("表", "列"))')
                tx.execute(f"INSERT OR REPLACE INTO {registry} ({cols})"
                           f" VALUES ({', '.join('?' for _ in ADDED_REGISTRY_COLUMNS)})",
                           [table, name, kind, "" if value is None else str(value),
                            _terminal(), datetime.now().strftime("%Y-%m-%d %H:%M:%S")])
    except source_db.SourceError as exc:
        return _write_failed(table, exc, file_key, "列を足す")

    log.info("マスタに列を足しました: %s / %s.%s (%s) 最初の値=%r %s行",
             file_key, table, name, kind, value, filled)
    said = f"{table} に列「{name}」({KIND_LABEL[kind]})を足しました"
    if value is not None:
        said += f"。いまある {filled}行 に「{value}」を入れました"
    return Result(True, said + "。")


# ==================================================================
# 表ごとの決まり ── 時間用
# ==================================================================
#: 直の境界時刻の表の列。**ここに入った値は、日報のほぼ全部の画面が読みます**
#: (何直か・どの日の日報か・催促・自動確定・作業時間の上限)。
SHIFT_KEY_COLUMN = "直"
SHIFT_TIME_COLUMNS: tuple[str, ...] = ("開始", "終了")
#: 読む側が引く直の鍵(`logic/shift`・`services/shift_check`)。
#: これ以外の直は**どこからも読まれない**ので、打っても効きません
def _shift_keys() -> tuple[str, ...]:
    from .logic.shift import FORM_KEYS

    return tuple(key for key, _ in FORM_KEYS)


SHIFT_KEYS: tuple[str, ...] = _shift_keys()      # logic/shift.FORM_KEYS の 1 か所から


def is_access_table(table: str) -> bool:
    """アクセス権限の表か(どのファイルにあっても。名前の全角半角・空白は問わない)。"""
    from .logic import access_rights

    return access_rights.fold(table) == access_rights.fold(access_rights.TABLE_NAME)


def _access_checks(path: Path, table: str) -> list[str]:
    """アクセス権限の表ぜんぶを点検する(**出している行だけでなく全部**)。"""
    from .logic import access_rights

    try:
        rows = source_db.read_table(path, table)
    except source_db.SourceError as exc:
        return [f"点検できませんでした: {exc}"]
    return [f.describe() for f in access_rights.inspect(rows)]


def _access_problem(clean: dict[str, Any]) -> str:
    """アクセス権限の「権限」に、**ラインのつもりで読めない値**を入れさせない。

    正規しか読まない(`logic/line_names`)ので、`L1` や `中板 3` を入れると、
    保存は通っても**黙って読まれません**。ここで断って、正規の書き方を言います。
    mode:field・作業長 のような**ラインではない値は通します**(スルーの決まり)。
    """
    from .logic import access_rights, line_names

    column = access_rights.rights_column(clean.keys())
    if column is None:
        return ""
    clean[column] = str(clean[column] or "").strip()
    for token in access_rights.tokens(clean[column]):
        mistake = line_names.check(token)
        if mistake:
            return (f"権限: {mistake}。ラインのつもりなら正規の書き方で入れてください"
                    "(設定・管理者 →「この端末のライン」→「ライン名の書き方」)。")
    return ""


def is_staff_table(file_key: str, table: str) -> bool:
    """班員名簿か(梱包資材マスタの)。"""
    from .config import SETTINGS
    return file_key == "material" and table == SETTINGS.staff_master_table


def _staff_checks(path: Path, table: str) -> list[str]:
    """班員名簿のオペレーター(AOP / ABOP / BOP)を全部の行で点検する(v4.15.0)。

    断りはしません(書いてある値が新しい区分のつもりかもしれないので)。読めない値は
    標準作業時間・梱包力で「区分なし」になる、と見える所に並べるだけです。
    """
    from .config import SETTINGS
    from .logic import operator

    column = SETTINGS.staff_operator_column
    try:
        if column not in source_db.columns(path, table):
            return []
        rows = source_db.read_table(path, table)
    except source_db.SourceError as exc:
        return [f"点検できませんでした: {exc}"]
    return [f.describe() for f in operator.inspect(rows, operator_column=column)]


def is_shift_table(file_key: str, table: str) -> bool:
    """直の境界時刻の表か(伝送用ファイルの「時間用」)。"""
    from .config import SETTINGS
    return file_key == "transmission" and table == SETTINGS.shift_time_table


def _table_problem(src: source_db.SourceConnection, file_key: str, table: str,
                   clean: dict[str, Any], *, row_key: Optional[int]) -> str:
    """表ごとの決まりに合うか。合わなければ断りの文。**`clean` は揃える。**

    いまは時間用だけです。型の無い列(文字)なので、`_clean` は何でも
    通します ── 「7時」「25:00」が通ると、直の時刻を読む画面がぜんぶ
    読み損ねます(読む側でも控えに落としますが、それは最後の砦です)。
    """
    if file_key == "vc":
        return _vc_problem(src, table, clean, row_key=row_key)
    if is_access_table(table):
        return _access_problem(clean)
    if not is_shift_table(file_key, table):
        return ""
    from .logic import shift as shift_logic

    for name in SHIFT_TIME_COLUMNS:
        if name not in clean:
            continue
        normalized = shift_logic.normalize_hhmm(clean[name])
        if not normalized:
            return (f"「{name}」は 07:00 のように 時:分 で入れてください"
                    "(0:00〜23:59)。")
        clean[name] = normalized

    if SHIFT_KEY_COLUMN not in clean:
        return ""
    key = unicodedata.normalize("NFKC", str(clean[SHIFT_KEY_COLUMN] or "")).strip()
    if key not in SHIFT_KEYS:
        return f"「{SHIFT_KEY_COLUMN}」は {'・'.join(SHIFT_KEYS)} のどれかで入れてください。"
    clean[SHIFT_KEY_COLUMN] = key

    # **同じ直を2行にしない。** 読む側は後の行で上書きするので、
    # 足したほうだけが黙って効き、前の行を直しても変わらなくなります
    quoted_key = source_db.quote_identifier(SHIFT_KEY_COLUMN)
    for other in src.query(
            f"SELECT rowid AS r, {quoted_key} AS k"
            f" FROM {source_db.quote_identifier(table)}"):
        other_key = unicodedata.normalize("NFKC", str(other["k"] or "")).strip()
        if other_key == key and other["r"] != row_key:
            return (f"直「{key}」の行はもうあります。同じ直が2行あると、"
                    "後の行だけが効きます ── その行のほうを直してください。")
    return ""


def _vc_problem(src: source_db.SourceConnection, table: str, clean: dict[str, Any],
                *, row_key: Optional[int]) -> str:
    """VC計算マスタの表ごとの決まり(vc-calculator `master_admin.MANAGED` の検査)。

    範囲(VC厚 > 0・有効は 0/1 など)は表の CHECK が守るので、ここは
    **決まった言葉しか効かない値**だけを見ます ── 違う言葉が入ると、
    読む側は黙って既定に倒します(直したのに効かない)。
    """
    from .vc import seed as vc_seed

    if table == "アプリ設定":
        found = (src.query('SELECT "キー" AS k FROM "アプリ設定" WHERE rowid = ?', [row_key])
                 if row_key is not None else [])
        key = str(found[0]["k"]) if found else ""
        if "キー" in clean and str(clean["キー"] or "") != key:
            return "アプリ設定のキーは変えられません(読む側が名前で引いています)。値を直してください。"
        if "値" in clean:
            value = unicodedata.normalize("NFKC", str(clean["値"] or "")).strip()
            if key == "肉厚の逆算" and value not in ("0", "1"):
                return "「肉厚の逆算」は 1(使う)か 0(使わない)で入れてください。"
            if key == "早見表の丸め" and value not in ("切り捨て", "四捨五入"):
                return "「早見表の丸め」は 切り捨て か 四捨五入 で入れてください。"
            clean["値"] = value
    if table == "早見表ブロック" and clean.get("枠色") is not None:
        tone = str(clean["枠色"]).strip()
        if tone not in vc_seed.TONES:
            return f"「枠色」は {' / '.join(vc_seed.TONES)} のどれかで入れてください。"
        clean["枠色"] = tone
    return ""


def after_write(result: Result, file_key: str, table: str, repo: Any) -> Result:
    """書いたあとに、**手元へ写してある値**を読み直す。

    参照マスタはたいてい毎回読みに行くので、直せばすぐ効きます。
    直の時刻だけは手元のDB(`shift_config`)へ写したものを見ていて、
    以前は**起動し直すか「時間マスタを取り込む」を押すまで効きません
    でした** ── 直した人には「直したのに変わらない」としか見えません。

    写せなかったときも、書いたことは取り消しません(元のファイルには
    もう書けています)。押す場所を言います。
    """
    if result.ok and is_access_table(table):
        return _after_access_write(result)
    if not result.ok or not is_shift_table(file_key, table):
        return result
    from .logic import shift as shift_logic
    from .services import shift_times

    try:
        times = shift_times.reload(repo)
    except Exception:                             # noqa: BLE001 - 書いたことは残す
        log.exception("直の時間を読み直せませんでした")
        times = None
    if times is None:
        return Result(True, result.message
                      + " ただし直の時間へはまだ効いていません。設定・管理者 →"
                        "「マスタ」→「直の境界時刻」の「時間マスタを取り込む」を"
                        "押してください。")
    message = (f"{result.message} 直の時間にもすぐ効かせました"
               f"({shift_times.describe(times)})。")
    malformed = shift_logic.malformed_shift_times(times)
    if malformed:
        message += (f" {'・'.join(malformed)} は時刻の形が違うので、"
                    "控えの時刻で動いています。")
    return Result(True, message, shift_times=[
        [key, start, end] for key, (start, end) in repo.get_shift_times().items()])


def _after_access_write(result: Result) -> Result:
    """アクセス権限を直したら、**読み直して**このPCの判定と点検を新しくする。

    当てはしません(ラインを当てるのは起動のときと「読み直す」だけ)。
    """
    from .services import access_rights

    try:
        status = access_rights.load()
    except Exception:                             # noqa: BLE001 - 書いたことは残す
        log.exception("アクセス権限を読み直せませんでした")
        return result
    message = f"{result.message} このPCの判定: {status.summary()}。"
    if status.findings:
        message += f" 表に読めない行が{len(status.findings)}行あります(上の一覧)。"
    return Result(True, message)


def table_notes(names: Iterable[str]) -> list[dict[str, str]]:
    """表の一覧に添える短い説明。**名前から読めないものだけ。**"""
    return [{"table": n, "note": TABLE_NOTES.get(n, "")} for n in names]
