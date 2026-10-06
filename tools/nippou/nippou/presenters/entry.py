"""日報入力画面のビューモデル

tkinter版 `ui/entry_grid.py` + `ui/app.py` の入力まわりに対応する。

【業務判断はここより下(logic/)が持つ】
このモジュールがやるのは3つだけ:

    1. ブラウザから来た平らな辞書を、行ごとの形へ組み直す
    2. `logic/` の純関数を呼んで、決まる値を決める(単重・包み数・
       排他制御・値の引き継ぎ)
    3. 画面が描ける形にして返す

**画面はここが返したものを写すだけ**にする。JS が「CON が 0 なら…」の
ような判断を持つと、テストの無い側に業務が分裂する。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional

from .. import constants, layout
from ..db.models import DetailRecord, HeaderRecord
from ..logic import (calculations, etc_marks, exclusions, g_course, input_rules,
                     navigation, reasons, work_time)

# 列の並びと見出しは `nippou/layout.py` が1つだけ持つ。**画面と紙で
# 同じものを読む** ── 別々に持っていたせいで、同じ欄が画面では「材質」、
# 紙では「材・調質」と呼ばれていた
COLUMNS = layout.COLUMNS
INTERLEAF_CHOICES = layout.INTERLEAF_CHOICES
column_groups = layout.column_groups

# 停止理由の記号を選ばせる欄。**打つのではなく選ばせる。**
#
# 記号は「レ」「G」「タ」「0」のような1文字で、覚えている人しか打てません。
# しかも文字の種類で分類が決まる(`CheckCharType`: 数値=管理ロス /
# カタカナ=突発・待ち / アルファベット=ハンドリング)ので、
# **打ち間違えると別の分類に入ったまま集計まで通ります。**
# 選ばせれば、その間違いは起きません。
STOP_CODE_FAMILIES: tuple[str, ...] = ("S", "SS", "STH")


def stop_choices() -> list[dict[str, Any]]:
    """停止理由の記号を、カテゴリごとにまとめて返す。

    出どころは伝送用ファイルの `作業停止時間内訳_1/_2/_3`
    (`access_bridge/stop_master`)。**読めなければ空で返す** ── マスタに
    届かないときに画面が出ないほうが困るので、そのときは自由入力に
    落ちる(テンプレートがそう描く)。
    """
    from ..access_bridge import stop_master

    try:
        by_category = stop_master.load_all_categories()
    except Exception:                             # noqa: BLE001 - 画面を落とさない
        return []
    out = []
    for category, items in by_category.items():
        rows = [{"code": r.code, "label": r.label,
                 # 選ぶときは記号だけだと分からない。並べて出す
                 "text": f"{r.code} {r.label}" if r.code else r.label}
                for r in items if r.code]
        if rows:
            out.append({"category": category, "reasons": rows})
    return out


def stop_codes(choices: list[dict[str, Any]]) -> set[str]:
    """一覧に出ている記号ぜんぶ。画面が「マスタに無い」を見分けるのに使う。"""
    return {r["code"] for group in choices for r in group["reasons"]}


# 「その他」に当たる停止理由の見分けかた。
#
# 紙の右上には **「ヨ：その他　（理由を記載）」** と刷ってあります ──
# 理由(header の `reason`)は**そのために置かれた欄**で、いつでも書くもの
# ではありません。決まった記号を選んだときにだけ「何があったのか」を
# 書き足します。
#
# 記号そのもの(紙では「ヨ」)を決め打ちにはしません。**記号はマスタが
# 決める**ので、マスタが差し替わると外れます。名前のほうで見分けます。
#
# 「他」1文字では拾いすぎます(「他社立会」なども当たる)。理由を書かせる
# 側に倒すと、書かなくてよい場面で毎回ポップアップが出るので、
# **紙に刷ってある言葉そのまま**にしておきます。
OTHER_REASON_MARKS: tuple[str, ...] = ("その他",)


def other_stop_codes(choices: list[dict[str, Any]]) -> set[str]:
    """「その他」に当たる記号。理由欄を開ける鍵になる。

    マスタが読めなければ空 ── そのときは停止理由そのものが自由入力に
    落ちるので、理由欄も開けたままにします(`reason_open` を参照)。
    """
    found = set()
    for group in choices:
        for reason in group["reasons"]:
            label = str(reason.get("label", ""))
            if any(mark in label for mark in OTHER_REASON_MARKS):
                found.add(reason["code"])
    return found

# 停止の3組。列見出しの色分けに使う(tokens.css の --stop-*)。
# **VBA `Stop_Distr` の分類とは別物**であることに注意 ── あちらは
# 入力されたコードの文字種で分けるので、欄の位置とは対応しない。
# ここは「1組目/2組目/3組目」を見分けるための色でしかない。
STOP_PAIRS: tuple[tuple[str, str, str], ...] = (
    ("S", "TH", "loss"),
    ("SS", "THS", "sudden"),
    ("STH", "THT", "handling"),
)

# 画面が扱う全ファミリ(UNI/TIM を含む)
ALL_FAMILIES: tuple[str, ...] = tuple(c.family for c in COLUMNS)

# 表には出ないが、行が持つ値。
#
# `others1`〜`others6` は紙の**印刷範囲外**(AN〜AS列)に出るもので、
# ロット番号を打つと自動で埋まります(用途コード / 用途名 / 納入先 /
# 包装仕様書No / コイル縦割 / コイル横縦割)。打つ欄ではないので表には
# 出しませんが、保存も反映もします。
#
# `hiki_no` は**この端末の中だけ**の控えです。1つのロットに引当が複数
# あるとき、どれを選んで埋めたのかを後から見るためのもので、紙にも
# Access にも欄がありません(共有へは送りません)。
#
# 綴りは DB の列名そのまま(小文字)。表の欄は大文字なので混ざりません。
EXTRA_FAMILIES: tuple[str, ...] = (
    "others1", "others2", "others3", "others4", "others5", "others6",
    # その行の理由(紙の「ヨ：その他（理由を記載）」)。表の列には出さず、
    # 表の下に「その他を選んだ行」だけ並べます ── 24列ある表にもう1列
    # 足すと、どの行も横に流れて読めなくなるので
    "reason",
    "hiki_no",
    # Gコースのロットだった印(`logic/g_course.ROW_FAMILY`)。**この端末の中だけ**。
    # 寸法の欄に「G」を出す(v4.17.0)
    "box_course",
)



# ------------------------------------------------------------------
# ヘッダー欄 ── **紙の上と下に分かれている**
#
# 6つを1か所に並べていたので、「上の入力欄は何なのか」が読めません
# でした。紙(梱包実績日報表)では別の場所にあり、性質も違います:
#
#   紙の1〜3行目(表の上)   作業者名 / 昼稼働 有・無 / ヨ：その他（理由を記載）
#                          … **打つ前に決まるもの**
#   紙の21〜26行目(表の下) 負荷計算後 / 直実績合計 ﾛｯﾄ数・枚数・重量
#                          … **打ち終わって出るもの(合計)**
#
# 合計を上に置くと、「まだ何も打っていないのに枚数欄がある」ように
# 見えます。紙と同じ場所へ戻します。
# ------------------------------------------------------------------

# 表の上。(名前, 見出し, 種別)
TOP_FIELDS: tuple[tuple[str, str, str], ...] = (
    ("worker", "作業者名(担当者)", "text"),
)

# 理由。**「その他」を選んだときだけ書く**ので、上とも合計とも別扱い
REASON_FIELD: tuple[str, str, str] = ("reason", "理由", "text")

# 表の下。(名前, 見出し, 種別, 計算で決まるか)
#
# 枚数と重量は `Weight計算` が出します(`calculations.apply_weights`)。
# **打つ欄ではない**ので読み取り専用にします ── 打てると、合計だけを
# 直して明細と食い違ったまま保存できてしまいます。
#
# ﾛｯﾄ数(`AlLotC`)と係数Lot数(`Allcoefficient`)は、ラインによって計算で
# 決まります ── `logic/load_factor.py`:
#
#     機側・NS1(コイル形状)  ﾛｯﾄ数・係数Lot数とも計算
#     AIM(板形状)            ﾛｯﾄ数だけ計算(v4.20.0)。係数Lot数は手入力(v4.18.0)
#     ほか(板形状)           どちらも手入力
#
# 係数Lot数は印刷シート27列目の係数処理ﾛｯﾄ数を足したもので、その27列目は
# 用途コードごとの重み(伝送用ファイルの `用途名負荷係数算出`)から入ります。
TOTAL_FIELDS: tuple[tuple[str, str, str, bool], ...] = (
    ("lot_count", "ﾛｯﾄ数", "num", False),
    ("count", "枚数", "num", True),
    ("weight_kg", "重量(Kg)", "num", True),
    ("coefficient_lot_count", "係数Lot数", "num", False),
)

#: ラインによって計算になる欄。**打てなくする**(計算と食い違わせない)
#:     ﾛｯﾄ数   … `load_factor.applies`(機側・NS1・AIM)
#:     係数Lot数 … `load_factor.counts_coefficient`(機側・NS1)
COIL_CALCULATED_FIELDS: tuple[str, ...] = ("lot_count", "coefficient_lot_count")


def total_fields(line: str = "") -> tuple[tuple[str, str, str, bool], ...]:
    """表の下の合計欄。**ラインによって計算になる欄が増えます。**

    計算で決まる欄(機側・NS1 は ﾛｯﾄ数・係数Lot数、AIM は ﾛｯﾄ数)は
    読み取り専用にします ── 打てると、合計だけ直して明細と食い違った
    まま保存できてしまいます(枚数・重量と同じ理由)。
    """
    from ..logic import load_factor

    computed = {"lot_count": load_factor.applies(line),
                "coefficient_lot_count": load_factor.counts_coefficient(line)}
    return tuple(
        (name, label, kind, calculated or computed.get(name, False))
        for name, label, kind, calculated in TOTAL_FIELDS)

# 保存・読み出しはこれまでどおり6つまとめて扱う
HEADER_FIELDS: tuple[tuple[str, str, str], ...] = (
    *TOP_FIELDS,
    *((name, label, kind) for name, label, kind, _calc in TOTAL_FIELDS),
    REASON_FIELD,
)

# 計算で決まる欄(画面が読み取り専用にする)
CALCULATED_HEADER_FIELDS: tuple[str, ...] = tuple(
    name for name, _l, _k, calculated in TOTAL_FIELDS if calculated)


@dataclass
class GridState:
    """画面1枚ぶんの入力内容。ブラウザから来たものをそのまま持つ。"""

    rows: dict[int, dict[str, str]] = field(default_factory=dict)
    header: dict[str, str] = field(default_factory=dict)
    day_shift: bool = False
    hdch: dict[str, bool] = field(default_factory=dict)
    checkbox: dict[str, bool] = field(default_factory=dict)

    def value(self, row: int, family: str) -> str:
        return self.rows.get(row, {}).get(family, "")

    def set(self, row: int, family: str, text: str) -> None:
        self.rows.setdefault(row, {})[family] = text


def empty_state() -> GridState:
    """空の入力内容。行は1..12まで必ず作る(画面が12行を描くため)。"""
    return GridState(
        rows={r: {f: "" for f in (*ALL_FAMILIES, *EXTRA_FAMILIES)}
              for r in range(1, constants.ROW_COUNT + 1)},
        header={name: "" for name, _, _ in HEADER_FIELDS},
        day_shift=False,
        hdch={name: False for name in constants.HDCH_NAMES},
        checkbox={name: False for name in constants.DETAIL_CHECKBOX_NAMES},
    )


def parse_state(payload: dict[str, Any]) -> GridState:
    """ブラウザから来た JSON を `GridState` にする。

    **知らないキーは捨てる。** 画面が送ってくるものだけを受け取り、
    行番号やファミリ名が想定外なら無視する(送られた値は誰にでも
    決められる)。
    """
    state = empty_state()

    raw_rows = payload.get("rows") or {}
    if isinstance(raw_rows, dict):
        for key, values in raw_rows.items():
            try:
                row = int(key)
            except (TypeError, ValueError):
                continue
            if not 1 <= row <= constants.ROW_COUNT or not isinstance(values, dict):
                continue
            for family in (*ALL_FAMILIES, *EXTRA_FAMILIES):
                if family in values:
                    # 打たれた形を欄の形へ直してから入れる ── 全角の
                    # ロットや8文字目は**ここで落とす**(`input_rules.
                    # normalize`)。画面の制限は貼り付け・IME・API直叩きで
                    # 通り抜けるので、入り口はここ1つにする
                    state.set(row, family,
                              input_rules.normalize(family, _text(values[family])))

    raw_header = payload.get("header") or {}
    if isinstance(raw_header, dict):
        for name, _, _ in HEADER_FIELDS:
            if name in raw_header:
                state.header[name] = _text(raw_header[name])

    state.day_shift = bool(payload.get("day_shift"))

    raw_boxes = payload.get("checks") or {}
    if isinstance(raw_boxes, dict):
        for name in constants.HDCH_NAMES:
            if name in raw_boxes:
                state.hdch[name] = bool(raw_boxes[name])
        for name in constants.DETAIL_CHECKBOX_NAMES:
            if name in raw_boxes:
                state.checkbox[name] = bool(raw_boxes[name])
    return state


def _text(value: Any) -> str:
    if value is None:
        return ""
    return str(value).strip()


# ------------------------------------------------------------------
# 業務ルールの適用
# ------------------------------------------------------------------
def recalculate(state: GridState, *, package_calc: bool,
                shift_times: Optional[dict[str, tuple[str, str]]] = None,
                current_shift: str = "",
                admin: bool = False) -> Optional["work_time.TimeProblem"]:
    """1画面ぶんの自動計算。**VBA がフォームでやっていた順そのまま。**

    VBA がシートへ直接打たせず、わざわざフォームを挟んでいたのは
    **入力した時点で計算と変換を済ませてから出力するため**でした。
    順は決まっていて、崩すと結果が変わります。

        1. 実績合計 枚数 = 個装単位 枚数 × 梱包単位 包数  (`Weight計算`)
        2. 単重         = 実績合計 重量 ÷ 実績合計 枚数   (`単重計算`)
        3. 実績合計 重量 = 実績合計 枚数 × 単重           (`Weight計算`)
        4. 作業時間     = 終了 − 開始 − 停止①②③        (`時間計算`)

    **枚数を先に決めるのが肝です。** 2 も 3 も枚数を使うので、古い枚数の
    まま走らせると、単重を消してしまったり重量が出なかったりします。
    2 と 3 は互いに逆向きで、**空いているほうだけが埋まります** ──
    重量を量って入れれば単重が出て、単重が分かっていれば重量が出ます。

    `state` をその場で書き換え、**時間計算が断ったときだけ**その理由を
    返します(VBA は `Exit Sub` して何も書かなかったので、時間の欄は
    埋めません)。断りは画面に出しますが、**入力そのものは受け付けます**
    ── 打っている途中で保存できなくなるほうが困ります。

    式は `logic/` を呼ぶだけです。ここへ書き写すと、テストのある側と分かれます。
    """
    # 1. 実績合計 枚数。**全ラインで「かけ算」。**
    #
    # VBA には `包み数計算`(枚数 ÷ 包数、LS/NS1/AIM のみ)もありましたが、
    # あれは入力中の Change で走るだけで、出力の直前に走る `Weight計算` が
    # 必ず「枚数 × 包数」で上書きしていました。実データ(日報管理の
    # T_日報明細_* 全ライン 2831 行)でも、**かけ算だけが成り立つ行が 75 行、
    # 割り算だけが成り立つ行は 0 行**でした。残るのはかけ算のほうです。
    #
    # **枚数と包数が両方そろっている行だけ**書き換えます。片方でも空なら
    # 打った値をそのまま残す ── VBA の `Weight計算` はボタンや出力の直前に
    # しか走らなかったので、手で入れた枚数がすぐ消えることはありませんでした。
    # こちらは欄を離れるたびに走るので、同じことをすると打った端から
    # 消えます。(実データでは枚数だけが入っている行は 0 行なので、
    # この保険が働く場面はほぼありません)
    for row in range(1, constants.ROW_COUNT + 1):
        if state.value(row, "MAI").strip() and state.value(row, "TUT").strip():
            state.set(row, "CON", calculations.package_total(
                mai=state.value(row, "MAI"), tut=state.value(row, "TUT")))

    # 2. 単重 = 重量 ÷ 枚数。**単重が空で、重量が入っている行だけ。**
    #
    # `単重計算` 自体は「単重がすでに入っていたら空にする」という枝を
    # 持っています(VBA のまま)。あれは重量欄を離れたときにしか走らな
    # かったので実害が小さかったのですが、こちらは欄を離れるたびに走る
    # ので、そのまま呼ぶと**手で入れた単重が消え、次の回に 重量÷枚数 で
    # 入れ直されます** ── 250.04314 と打ったものが 250.03 に化けます。
    #
    # そこで呼ぶ場面を絞ります。2 と 3 は互いに逆向きなので、
    # **空いているほうだけを埋める**のが筋です:
    #
    #     単重が空 + 重量あり → 単重を出す      (ここ)
    #     単重あり + 重量が空 → 重量を出す      (次の 3)
    #
    # 単重だけ分かっていて重量はこれから、というのは普通の使い方で、
    # 実データでも 74 行ありました(単重あり・重量なし)。
    for row in range(1, constants.ROW_COUNT + 1):
        if state.value(row, "UNI").strip() or not state.value(row, "WEI").strip():
            continue
        state.set(row, "UNI", calculations.unit_weight(
            con=state.value(row, "CON"),
            wei=state.value(row, "WEI"),
            uni="",
            lot=state.value(row, "LOT"),
        ))

    # 3. 単重から作業重量。**直の合計もここで決まる**(VBA `AlCount`/`AlWeight`)
    count, weight = calculations.apply_weights(
        state.rows, row_count=constants.ROW_COUNT)
    state.header["count"] = count
    state.header["weight_kg"] = weight

    # 4. 作業時間。断られたら時間の欄は触らない(VBA の `Exit Sub`)
    #
    # **断られたら全行を空にします。** VBA も先頭で全部消してから計算し、
    # 途中で見つけたら `Exit Sub` していたので、断られた時点でどの行の
    # 作業時間も残りません。前の値を残すと、直したつもりの無いところに
    # 古い時間が居座って、そのまま保存されます。
    limit = work_time.shift_limit(shift_times or {}, current_shift, admin=admin)
    result = work_time.compute(state.rows, limit_minutes=limit)
    for row in range(1, constants.ROW_COUNT + 1):
        state.set(row, "TIM", result.times.get(row, "") if result.ok else "")
    return result.problem


def time_problems(state: GridState, *,
                  shift_times: Optional[dict[str, tuple[str, str]]] = None,
                  current_shift: str = "",
                  admin: bool = False) -> list["work_time.TimeProblem"]:
    """**直っていない行を全部**。印を付けるのと、保存を止めるのに使う。

    `recalculate` が返すのは VBA どおり「最初の1つ」で、計算を止める
    ためのものです。こちらは**12行のどこが直っていないか**を出します。
    """
    limit = work_time.shift_limit(shift_times or {}, current_shift, admin=admin)
    return work_time.problems(state.rows, limit_minutes=limit)


# ------------------------------------------------------------------
# 紙1枚の埋まりぐあい
#
# 紙は12行しかありません。VBA は使い切ると別のシート
# (`temp(2)` / `temp(3)`)を「新規発行」して続けていました
# ── 使い切ったことが分からないと、13行目を打とうとして手が止まります。
# ------------------------------------------------------------------
#: 「打ってある」と見なす欄。**ロットが無くても時刻だけの行はある**
#: (全停入力の1行目など)。数え方そのものは `logic/pages.row_state`
_USED_MARKS = ("LOT", "KZ", "KH", "SZ", "SH", "MAI", "TUT", "WEI")


def _row_values(state: GridState, row: int) -> dict[str, str]:
    return {f: state.value(row, f) for f in _USED_MARKS}


def row_used(state: GridState, row: int) -> bool:
    """その行を紙の行として数えるか。`used_rows` と同じ見方をする。

    **開始時刻だけの行は数えません**(v4.8.0)。11行目の終了を打つと、
    その時刻が12行目の開始へ自動で写ります ── それを「使った」と数えると、
    11行しか打っていないのに「12行を使い切りました」と出て、次のページも
    出せていました(`logic/pages.row_state`)。
    """
    from ..logic import pages

    return pages.row_counts(pages.row_state(_row_values(state, row)))


def g_marks(state: GridState) -> dict[str, dict[str, str]]:
    """Gコースだった行の印(v4.17.0)。**行の控え `box_course` から作る。**

        日報入力もGWもGのコースがあったことはわかるようにしてください

    寸法の欄の隅に「G」、乗せると「Gコース(GSS)のロット ── 寸法は BOX最終実績」。
    控えは行が持っているので、保存して開き直しても出ます。
    """
    marks = {}
    for row in range(1, constants.ROW_COUNT + 1):
        mark = g_course.row_mark(state.value(row, g_course.ROW_FAMILY))
        if mark is not None:
            marks[str(row)] = mark
    return marks


def used_rows(state: GridState) -> int:
    """何か打ってある行の数。**ロット№で数える。**

    ロットが無くても時刻の入った行(全停入力の1行目など)はあるので、
    ロット・終了・梱包数量・重量のどれかが入っていれば「使った」と見ます。
    開始時刻だけの行は数えません(上の行から写っただけのことが多い)。
    """
    return sum(1 for row in range(1, constants.ROW_COUNT + 1)
               if row_used(state, row))


def new_page_check(state: GridState):
    """次のページを出してよいか(`logic/pages.new_page_check`)。"""
    from ..logic import pages

    return pages.new_page_check(
        {r: _row_values(state, r) for r in range(1, constants.ROW_COUNT + 1)},
        constants.ROW_COUNT)


def sheet_full(state: GridState) -> bool:
    """12行目まで打ち終わったか。終わっていたら新しいページを出せる。"""
    return new_page_check(state).ready


# ------------------------------------------------------------------
# ページの案内 (v4.8.0 → v4.9.0)
#
#     12行目まで埋まった際は次ページをどう出すかの案内
#     また戻って直したい際の案内
#
# 表のすぐ上に、**いま何をすればよいか**を出します。どの場面でどう言うかは
# ここで決め、画面は写すだけです(`views/entry.js` の `paintPageGuide`)。
#
# 【v4.9.0 ── 1回に1行】
#     説明を一気に出しすぎです / はじめから4行長文で出すと読まないやつが大半です
#     埋まった→次ページで発行 引継ぎ内容 / 赤い行があった→確かめ / 発行後→戻り方を出す
#
# v4.8.0 は12行目まで済んだ時点で手順を4つ並べていました。**その場面で
# 要る一言だけ**を出します(くわしくはマウスを乗せると `detail` が出る):
#
#     打っている途中   … 何も出さない(表の上の「8/12行使用(あと4行)」だけ)
#     12行目まで済んだ … 「次ページ発行」で第N+1ページへ・引き継ぐもの   [ここへ]
#     赤い行がある     … 赤い行を直してから(直すところが残っていると出せない)
#     出した直後       … 前のページの直し方(空の新しいページを開いているあいだ) [ページへ]
#     前のページを直す … 直したら「保存(確定)」、続きは「最新のページに戻る」 [最新のページに戻る]
# ------------------------------------------------------------------
GUIDE_NONE = "none"
GUIDE_FILLING = "filling"
GUIDE_READY = "ready"
GUIDE_FIX = "fix"
GUIDE_ISSUED = "issued"
GUIDE_EDITING = "editing"

#: 案内の箱を出す場面(ほかは表の上の一言だけ)
GUIDE_SHOWN = (GUIDE_READY, GUIDE_FIX, GUIDE_ISSUED, GUIDE_EDITING)

#: 次のページへ引き継ぐもの(一言に添える)
CARRIED_OVER = "作業者・昼稼働は引き継ぎます"


def page_guide(state: GridState, *, page: int, page_count: int, recall: bool,
               recall_other: bool = False, read_only: bool = False,
               bad_rows: int = 0) -> dict[str, Any]:
    """表の上の一言(`rows_text`)と、出すなら案内の一行(`text`)。

    `bad_rows` は時間の断りが残っている行(画面で赤い行)の数。12行目まで
    済んでいても、赤い行があれば次のページは出せないので、そちらを先に言う。
    """
    check = new_page_check(state)
    count = constants.ROW_COUNT
    latest = max(page_count or 1, page or 1)
    left = f"(あと{count - check.used}行)" if not check.ready else ""
    out: dict[str, Any] = {
        "mode": GUIDE_FILLING, "ready": False, "reason": check.refusal,
        "used": check.used, "rows_text": f"{check.used}/{count}行使用{left}",
        "text": "", "detail": "", "action": "", "confirm": "",
        "gaps": list(check.gaps),
    }
    if read_only or recall_other:
        out["mode"] = GUIDE_NONE
        out["reason"] = "過去データを開いているあいだは、新しいページを出せません。"
        return out
    if recall:
        # **同じ直の前のページを直している。** 新しいページはここからは出さない
        # (出すと、どのページの次なのかが押した人に分からない)
        out.update(
            mode=GUIDE_EDITING,
            reason=("前のページを開いているあいだは、新しいページを出せません。"
                    "先に「最新のページに戻る」を押してください。"),
            text=(f"第{page}ページを直しています ─ 直したら「保存(確定)」、"
                  "続きは「最新のページに戻る」"),
            detail=(f"「最新のページに戻る」で第{latest}ページへ戻ります(戻る前に、"
                    "このページも保存します)。ほかのページは表の下の「ページ」で選びます"),
            action="back")
        return out
    if check.ready and bad_rows:
        out.update(
            mode=GUIDE_FIX, ready=False,
            reason=f"赤い行が{bad_rows}行あります。直してから「次ページ発行」を押してください。",
            text=f"12行目まで埋まりました ─ 先に赤い行({bad_rows}行)を直してください",
            detail=("直すところが残っていると、次のページは出せません"
                    "(行の赤い印にマウスを乗せると理由が出ます)"))
        return out
    if check.ready:
        next_page = latest + 1
        out.update(
            mode=GUIDE_READY, ready=True, reason="",
            text=f"12行目まで埋まりました ─「次ページ発行」で第{next_page}ページへ({CARRIED_OVER})",
            detail=(f"押すと第{page}ページを保存して、空の第{next_page}ページを開きます。"
                    "直すところが残っていると出せません"),
            action="goto-new-page",
            confirm=_new_page_confirm(page, next_page, check))
        return out
    if (page or 1) > 1 and (page or 1) >= latest and check.used == 0:
        # **出した直後。** 空の新しいページを開いているあいだだけ、戻り方を言う
        # (打ち始めたら消える ── 要るのは出した直後に気づいたとき)
        out.update(
            mode=GUIDE_ISSUED,
            text=(f"第{page}ページを打てます ─ 前のページを直すときは、"
                  "表の下の「ページ」で番号を選びます"),
            detail=("直したら「保存(確定)」、続きは「最新のページに戻る」で"
                    "このページへ戻ります"),
            action="goto-pages")
    return out


def guide_after_findings(guide: dict[str, Any], count: int) -> dict[str, Any]:
    """次ページ発行が**直すところで止まった**ときの一言(`fix` にする)。"""
    out = dict(guide)
    out.update(mode=GUIDE_FIX, ready=False,
               reason=f"直すところが{count}件あります。直してから「次ページ発行」を押してください。",
               text=f"12行目まで埋まりました ─ 先に直すところ({count}件)を直してください",
               detail="直すところは上の帯に出ています。直すと「次ページ発行」を押せます",
               action="", confirm="")
    return out


def _new_page_confirm(page: int, next_page: int, check) -> str:
    """押したときに1回だけ聞く文。**聞くことは全部ここにまとめる**
    (空いた行・いつもより多いページ・何が起きるか)── 2回続けて聞くと、
    1回目の「OK」の勢いで2回目を読まずに押します。"""
    from ..logic import pages

    parts = [check.gap_question(), pages.new_page_warning(page),
             pages.new_page_confirm(page, next_page)]
    return "\n\n".join(p for p in parts if p)


def sheet_has_anything(state: GridState) -> bool:
    """紙に**1文字でも**打ってあるか。

    `used_rows` とは別のことを訊いています ── あちらは「12行のうち何行
    使ったか」で、**停止だけの行や理由だけの行は数えません**(紙の行数を
    数えるものなので、それでよい)。

    こちらは「保存する中身があるか」です。停止を1つ入れただけの行も、
    理由を書いただけの行も**中身**なので、行の欄をぜんぶ見ます。
    作業者は見ません ── 作業者を選んだだけでは、まだ何もしていません。
    """
    families = tuple(ALL_FAMILIES) + tuple(EXTRA_FAMILIES) + ("reason",)
    return any(state.value(row, family).strip()
               for row in range(1, constants.ROW_COUNT + 1)
               for family in families)


# ------------------------------------------------------------------
# 理由(紙の「ヨ：その他（理由を記載）」)
# ------------------------------------------------------------------
def reason_rows(state: GridState, other_codes: set[str]) -> list[int]:
    """「その他」の記号を選んである行。理由を書く相手が誰かを示す。"""
    if not other_codes:
        return []
    hits = []
    for row in range(1, constants.ROW_COUNT + 1):
        if any(state.value(row, family).strip() in other_codes
               for family in STOP_CODE_FAMILIES):
            hits.append(row)
    return hits


def reason_entries(state: GridState,
                   other_codes: set[str]) -> list[dict[str, Any]]:
    """理由を書く行の一覧。**行ごとに1つ。**

    出すのは「その他」を選んだ行と、**すでに理由が書いてある行**です
    ── 記号を選び直して「その他」でなくなっても、書いた文字は黙って
    消しません(消すかどうかは打った人が決めること)。

    並びは行の順。紙を上から追う順です。
    """
    out: list[dict[str, Any]] = []
    for row in range(1, constants.ROW_COUNT + 1):
        values = state.rows.get(row, {})
        text = (values.get("reason") or "").strip()
        wanted = reasons.needs_reason(values, other_codes)
        if not (wanted or text):
            continue
        out.append({
            "row": row,
            "label": reasons.label(row),
            "text": text,
            # 「その他」を選んでいるのに空 ── 画面が促す(断りはしない)
            "needed": wanted,
            "empty": wanted and not text,
            # どの記号で「その他」を選んだか。書く手がかりになる
            "lot": (values.get("LOT") or "").strip(),
        })
    return out


def reason_open(state: GridState, other_codes: set[str]) -> bool:
    """理由欄を開けてよいか。

    **マスタが読めないときは開けたままにします。** そのときは停止理由も
    自由入力に落ちているので(テンプレートがそう描く)、こちらだけ閉じると
    書く手立てが無くなります ── 閉じるのは「その他が選べる状態で、まだ
    選んでいない」ときだけです。
    """
    if not other_codes:
        return True
    return bool(reason_rows(state, other_codes))


def apply_exclusion(state: GridState, name: str) -> GridState:
    """チェックボックスの排他制御。`logic/exclusions.py` を呼ぶだけ。"""
    def get(n: str) -> bool:
        return state.hdch[n] if n in state.hdch else state.checkbox.get(n, False)

    def set_(n: str, v: bool) -> None:
        if n in state.hdch:
            state.hdch[n] = v
        else:
            state.checkbox[n] = v

    exclusions.apply_exclusion(name, get, set_)
    return state


def carry_end_time(state: GridState, row: int, *, day_temp: str,
                   shift_end_times: dict[str, tuple[str, str]],
                   carried_rows: set[int] | None = None) -> set[int]:
    """終了時刻を次の行の開始時刻へ引き継ぐ(`Same_Text` 相当)。

    ``carried_rows`` は「**ツールが入れた開始時刻が、まだ人に触られずに
    残っている行**」。画面が持っていて、人がその欄を打てば落とします。
    引き継ぎが断られたとき、印の付いている行だけ**引っ込めます** ──
    判断は `logic/navigation.carry_decision`。

    返すのは新しい印の集合。画面はこれを持ち直します。
    """
    marks = set(carried_rows or ())
    decision = navigation.carry_decision(
        row=row,
        sz_text=state.value(row, "SZ"),
        sh_text=state.value(row, "SH"),
        day_temp=day_temp,
        shift_end_times=shift_end_times,
        carried_rows=marks,
    )
    if decision.writes:
        state.set(decision.row, "KZ", decision.kz)
        state.set(decision.row, "KH", decision.kh)
        marks.add(decision.row)
    elif decision.clears:
        state.set(decision.row, "KZ", "")
        state.set(decision.row, "KH", "")
        marks.discard(decision.row)
    return marks


# ------------------------------------------------------------------
# 保存する形へ
# ------------------------------------------------------------------
def to_records(state: GridState, report_date: str, line: str, shift: str,
               page: int) -> tuple[HeaderRecord, list[DetailRecord]]:
    """`GridState` を保存用のレコードにする。

    明細は**空行も含めて12行すべて**返す。tkinter版の
    `EntryGrid.collect_details` と同じで、行番号と入力内容の対応を
    保存側で崩さないため。

    書き出す欄は `ALL_FAMILIES`(=`layout.COLUMNS`)から引きます。
    **以前は `constants.ROW_FIELD_FAMILIES` を使っていて、そこに合紙(AI)が
    入っていませんでした** ── DBには `ai` 列があるのに、何を入れても
    保存されない状態でした。`ROW_FIELD_FAMILIES` は `Point_Change`
    (行の色を変える)のための並びで、保存する欄の一覧ではありません。
    """
    header = HeaderRecord(
        report_date=report_date, line=line, shift=shift, page=page,
        worker=state.header.get("worker", ""),
        day_shift="有" if state.day_shift else "無",
        count=state.header.get("count", ""),
        weight_kg=state.header.get("weight_kg", ""),
        lot_count=state.header.get("lot_count", ""),
        coefficient_lot_count=state.header.get("coefficient_lot_count", ""),
        # **紙と共有へは1つにまとめて出す。** 欄が1つしかない紙に合わせる
        # ためで、持ち方(行ごと)とは別の話です(`logic/reasons.combine`)。
        # 行番号の付かない古い理由があれば、後ろに残します ── 行が
        # 分からないというだけで、書いてあることは捨てません
        reason=_combined_reason(state),
    )

    details: list[DetailRecord] = []
    for row in range(1, constants.ROW_COUNT + 1):
        values = state.rows.get(row, {})
        details.append(DetailRecord(
            report_date=report_date, line=line, shift=shift, page=page, row_no=row,
            **{family.lower(): values.get(family, "") for family in ALL_FAMILIES},
            # 表に出ない欄(印刷範囲外の6つ + 引当番号)も一緒に書く
            **{family: values.get(family, "") for family in EXTRA_FAMILIES},
        ))
    return header, details


def _combined_reason(state: GridState) -> str:
    """紙と共有へ出す理由の1行。**まとめ方は `logic/reasons` が持つ。**"""
    by_row = {row: state.value(row, "reason")
              for row in range(1, constants.ROW_COUNT + 1)}
    combined = reasons.combine(by_row)
    # 行番号の付かない古い理由。**捨てない**(行が分からないだけ)
    leftover = (state.header.get("reason") or "").strip()
    return reasons.SEPARATOR.join(t for t in (combined, leftover) if t)


def from_records(header: HeaderRecord, details: list[DetailRecord]) -> GridState:
    """保存済みのレコードを画面の形へ戻す(過去データ呼出)。"""
    state = empty_state()
    state.header.update({
        "worker": header.worker, "count": header.count,
        "weight_kg": header.weight_kg, "lot_count": header.lot_count,
        "coefficient_lot_count": header.coefficient_lot_count,
        "reason": "",
    })
    state.day_shift = header.day_shift == "有"
    for detail in details:
        if not 1 <= detail.row_no <= constants.ROW_COUNT:
            continue
        for family in ALL_FAMILIES:
            state.set(detail.row_no, family, getattr(detail, family.lower(), ""))
        for family in EXTRA_FAMILIES:
            state.set(detail.row_no, family, getattr(detail, family, ""))

    # **行ごとに持つ前のデータを読めるようにする。**
    #
    # 昔のページは理由が頭(`daily_header.reason`)に1つだけ入っています。
    # 行番号が付いていれば行へ戻し、付いていなければ「どの行か分からない
    # 理由」として頭に残します ── どちらも捨てません。
    # 明細に理由が入っている行は**そちらが正**(あとから直したもの)。
    found = reasons.split(header.reason)
    state.header["reason"] = found.pop(0, "")
    for row, text in found.items():
        if 1 <= row <= constants.ROW_COUNT and not state.value(row, "reason"):
            state.set(row, "reason", text)
    return state


# ------------------------------------------------------------------
# 画面へ渡す形
# ------------------------------------------------------------------
def steps(state: GridState, *, saved: bool = False) -> list[dict[str, Any]]:
    """作業の順序。**画面がこれを帯として出す。**

    日報入力の画面には、ライン・作業者・明細・保存が同じ重さで並んで
    いました。どれから触るのかは現場では決まっているのに、画面がそれを
    示していないので、初めての人は押す順を覚えるまで迷います。

    やることの中身は変わりません ── 順番に番号を振って、**いまどこか**
    を示すだけです。飛ばして触れないようにはしません(直しに戻ることが
    あるので、閉じると邪魔になる)。
    """
    has_worker = bool(state.header.get("worker", "").strip())
    has_rows = used_rows(state) > 0
    # **ラインは順番に入れません。** 据え付けのときに1度決めるもので、
    # 毎直やることではありません ── 手順の1番目に置くと「まず選ぶもの」
    # に見えて、押し間違えたまま打ち始められます(`app/routes/entry.
    # change_line` の説明)。いま何かは帯にいつも出ています
    order = [
        {"key": "worker", "label": "作業者", "note": "「作業者を選ぶ」から",
         "done": has_worker},
        {"key": "rows", "label": "日報を入力", "note": "上の行から順に",
         "done": has_rows},
        {"key": "save", "label": "保存(確定)", "note": "12行を使い切ったら新しいページ",
         "done": saved},
    ]
    # **いまどこか**は「まだ済んでいない最初のもの」。全部済んでいれば最後
    current = next((s["key"] for s in order if not s.get("done")),
                   order[-1]["key"])
    for item in order:
        item["current"] = item["key"] == current
    return order


def view_model(state: GridState, *, report_date: str, line: str, shift: str,
               page: int, page_count: int, recall: bool,
               package_calc: bool, message: str = "",
               focus: str = "",
               pages: Optional[list[int]] = None,
               recall_other: bool = False,
               read_only: bool = False,
               other_codes: Optional[set[str]] = None,
               bad_rows: int = 0) -> dict[str, Any]:
    """画面ぜんぶ。**差分ではなく毎回一式を返す。**

    この規模(数KB)で差分を使う利点は無く、画面側の不整合バグの温床に
    なる。JS は受け取ったものを写すだけでよくなる。
    """
    codes = other_codes if other_codes is not None else set()
    rows_used = used_rows(state)
    guide = page_guide(state, page=page, page_count=page_count, recall=recall,
                       recall_other=recall_other, read_only=read_only, bad_rows=bad_rows)
    return {
        # 紙1枚の埋まりぐあい。**12行しかない**ので、残りが読めないと
        # 13行目を打とうとして手が止まる
        "used_rows": rows_used,
        "row_count": constants.ROW_COUNT,
        "rows_left": constants.ROW_COUNT - rows_used,
        # **12行目が終了まで入ったか**(= 次のページを出せるか)。v4.8.0 から
        # 行の数ではなく12行目の中身で見る(`logic/pages.new_page_check`)
        "sheet_full": guide["ready"],
        # 表の上の一行と、次ページの出し方 / 前のページの直し方の案内
        "page_guide": guide,
        # 重量の添え書き(紙の「4379.4 Kg / 4.38 T」)
        "weight_t": calculations.tons(state.header.get("weight_kg", "")),
        # 理由は「その他」を選んだときだけ書く(紙の「ヨ：その他（理由を記載）」)。
        # **行ごとに1つ**持ち、紙と共有へ出すときだけ1つにまとめます
        "reason_open": reason_open(state, codes),
        "reason_rows": reason_rows(state, codes),
        "reason_entries": reason_entries(state, codes),
        # 紙と共有へ出る形。画面にも出して、何が載るのかを見せる
        "reason_combined": _combined_reason(state),
        # 行番号の分からない古い理由(行ごとに持つ前のデータ)
        "reason_legacy": (state.header.get("reason") or "").strip(),
        # 作業の順序。**画面はこれを写すだけ**
        "steps": steps(state),
        # etc欄の印。**行ごとに etc の文字から読み直す**(VBA
        # `CommandLook_For`)。ボタンは状態を持たない
        "marks": {str(r): etc_marks.active(state.value(r, "ET"))
                  for r in range(1, constants.ROW_COUNT + 1)},
        # Gコースの行(寸法の欄の「G」)。**行の控え `box_course` から読み直す**(v4.17.0)
        "g_marks": g_marks(state),
        "report_date": report_date,
        "line": line,
        "shift": shift,
        "page": page,
        "page_count": page_count,
        # この直で**保存されているページ**。ページ1と3だけ、はありうるので
        # 「1から page_count まで」では作らない
        "pages": list(pages or []),
        "recall": recall,
        # 開いているのが**他の直**か。同じ直のページを戻っているだけなら、
        # 「過去データを開いています」は言い過ぎ ── 自分がさっき打った
        # 紙を直しているだけで、言葉が強いと押した人が不安になる
        "recall_other": recall_other,
        # ------------------------------------------------------------
        # **見るだけ。** 他の直の過去データを、管理者モードでないまま
        # 開いている状態です。
        #
        # 「管理者モード終わった後でも1直データが開いてたらいじれるのは
        # NG（過去データにはなっている）」と言われたところです。保存は
        # 前から 403 で断っていましたが、**打つことはできました** ──
        # 断られるのは押したときなので、それまでは直せているつもりで
        # 手を動かします。直の変わり目で管理者モードが自動で外れると、
        # 誰も何も操作していないのにこの状態になります。
        #
        # 打てなくするのは画面の話で、関門はサーバ(`/api/entry/save` の
        # 403)のままです ── 見た目の細工は戻る/進むや古いタブをすり抜け
        # ます。
        # ------------------------------------------------------------
        "read_only": read_only,
        "package_calc": package_calc,
        "rows": {str(r): dict(state.rows.get(r, {})) for r in
                 range(1, constants.ROW_COUNT + 1)},
        "header": dict(state.header),
        "day_shift": state.day_shift,
        "checks": {**state.hdch, **state.checkbox},
        # 画面が焦点を移す先(`Same_Text` の引き継ぎ後など)。空なら動かさない
        "focus": focus,
        # 出す文言は**サーバが決める**
        "message": message,
    }
