"""日報の列並び ── **画面と紙で1つだけ持つ**

【なぜ独立した層に置くのか】
入力画面(`presenters/entry.py`)と印刷用HTML(`reporting/print_format.py`)
が、それぞれ自前の列並びと見出しを持っていました。結果、同じ欄が画面では
「材質」、紙では「材・調質」と呼ばれ、画面には「包み数(計)」があるのに
紙では「実績合計 枚数」になっている、という状態でした。

現場の正は**紙(梱包実績日報表)**です。入力する人は紙を見ながら打つので、
画面の見出しが紙と違うと、毎回頭の中で読み替えることになります。だから

    紙の並びと見出しをここに1つだけ書き、画面も紙もここを読む

ことにします。片方だけ直して食い違う、が起きなくなります。

【紙の列 (提出された座標表より)】
    A   ロット№                 B  材・調質
    C:D 製品寸法 厚×幅×丈       E  検入枚数
    F:I 梱包作業時間 (F:G 開始時間 時・分 / H:I 終了時間 時・分)
    J   作業人数                 K  合紙 (有/無)
    L:M 梱包数量 (L 個装単位 枚数 / M 梱包単位 包数)
    N   ＶＣ種別 両面・片面      O  etc 反転・EX etc
    P:Q 作業停止① 記号・時間(分)
    R:S 作業停止② 記号・時間(分)
    T:U 作業停止③ 記号・時間(分)
    V:W 実績合計 枚数・重量

【紙に載らない欄】
X列(作業時間(分))と Y列(単重)は印刷範囲の外にあります。計算結果を
置いてある場所で、刷ったときには出てきません。画面では出しますが、
**入力する列と同じ見た目にはしません** ── 同じに見えると「刷ったのに
出てこない」と受け取られます。
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Column:
    """列1つ。画面も紙もこれを読む。"""

    family: str           # 画面の欄の名前(大文字)。DBの列は小文字
    label: str            # 下段の見出し。**紙の言葉**(紙とCSVがこれを使う)
    group: str            # 上段の見出し(空なら結合しない)
    width: int            # 画面での幅の目安px
    kind: str             # text / num / calc / select
    outside_print: bool = False   # 紙に載らない(印刷範囲の外)
    #: 打つ画面だけで使う見出し。**空なら `label` をそのまま使います。**
    #:
    #: 【なぜ画面だけ別の名前を持てるようにしたのか】
    #: `label` は**紙の言葉**です(このファイルの冒頭)。紙は現物なので
    #: こちらから変えられませんし、CSVの列名も `label` から作るので、
    #: 変えると**去年出したCSVが取り込めなくなります**
    #: (`services/csv_import.py` はこの名前で列を探します)。
    #:
    #: 一方、打つ欄の見出しは**打つ人の言葉**であるほうが速い ──
    #: 現場が口にするのも、引き先(`LS4LOT` / `SIKALOT`)の列名も
    #: `LOTNO` でした。紙とCSVは動かさず、画面だけ合わせます。
    screen_label: str = ""

    @property
    def field(self) -> str:
        """DB(`DetailRecord`)側の属性名。"""
        return self.family.lower()

    @property
    def screen_name(self) -> str:
        """打つ画面での下段の見出し。"""
        return self.screen_label or self.label


COLUMNS: tuple[Column, ...] = (
    # 紙とCSVは「ロット№」のまま、**打つ画面だけ LOTNO**。
    # 現場が口にするのも、引き先(`LS4LOT` / `SIKALOT`)の列名も LOTNO
    # です(`screen_label` の説明)。
    # 幅は**中身に合わせる**: LOTNO は最大7桁なので7文字ぎりぎり(余った幅は
    # 寸法へ回す)。寸法は「20.000×1528.0×13053.0」(丈5桁)まで見切れない幅
    Column("LOT", "ロット№", "", 68, "text", screen_label="LOTNO"),
    Column("ZAI", "材・調質", "", 76, "text"),
    Column("SIZ", "厚×幅×丈", "製品寸法", 180, "text"),   # Gコースの印のぶん広い(v4.17.0)
    Column("KEN", "検入枚数", "", 56, "num"),
    Column("KZ", "開始 時", "梱包作業時間", 44, "num"),
    Column("KH", "開始 分", "梱包作業時間", 44, "num"),
    Column("SZ", "終了 時", "梱包作業時間", 44, "num"),
    Column("SH", "終了 分", "梱包作業時間", 44, "num"),
    Column("HIT", "作業人数", "", 48, "num"),
    # 合紙。**DBには前からあったのに、画面にも紙にも出ていなかった**
    # (`daily_detail.ai` / 日報明細の `AI` 列)
    Column("AI", "合紙", "", 56, "select"),
    Column("MAI", "個装単位 枚数", "梱包数量", 64, "num"),
    Column("TUT", "梱包単位 包数", "梱包数量", 64, "num"),
    Column("VC", "両面・片面", "ＶＣ種別", 108, "text"),
    Column("ET", "反転・EX etc", "etc", 120, "text"),
    Column("S", "記号", "作業停止①", 92, "select"),
    Column("TH", "時間(分)", "作業停止①", 52, "num"),
    Column("SS", "記号", "作業停止②", 92, "select"),
    Column("THS", "時間(分)", "作業停止②", 52, "num"),
    Column("STH", "記号", "作業停止③", 92, "select"),
    Column("THT", "時間(分)", "作業停止③", 52, "num"),
    Column("CON", "枚数", "実績合計", 60, "num"),
    Column("WEI", "重量", "実績合計", 72, "num"),
    # ここから先は紙に載らない(印刷範囲の外)
    #
    # 作業時間は**サーバが決めるので読み取り専用**(`時間計算`)。単重は
    # **打てる** ── 単重だけ分かっていて重量はこれから出す、というのが
    # 普通の使い方で(実データで 74 行)、読み取り専用にすると
    # 「単重から作業重量を出す」ができなくなる
    Column("TIM", "作業時間(分)", "印刷範囲外", 68, "calc", outside_print=True),
    Column("UNI", "単重", "印刷範囲外", 76, "num", outside_print=True),
)

#: 紙に載る列だけ。印刷用HTMLはこちらを使う
PRINT_COLUMNS: tuple[Column, ...] = tuple(
    c for c in COLUMNS if not c.outside_print)

# ----------------------------------------------------------------------
# 印刷範囲の**さらに外**にある列 (紙の AN〜AS)
#
# 表にも紙にも出ませんが、**集計には出ます。** VBA の `Agg_OutPut` は
# 印刷シートの 40〜45列(AN〜AS)を `Storage_Sh_Position` で拾って、集計
# シートの 31〜34 / 43〜44 列へ書いていました ── 納入先や包装仕様書No は
# そこでしか見られません。
#
# 名前は**紙の見出し行(AN7〜AS7)そのまま**にしてあります。集計シート側は
# 「包装仕様NO」「コイル横割」と少し違う書き方をしていましたが、
# この移植では紙を正とする決まりなので(このファイルの冒頭)、紙に合わせます。
#
# ロット番号を打つと自動で埋まります(`logic/lot_fill.others`)。打つ欄では
# ないので画面の表には出しませんが、保存も共有への反映も集計もします。
# ----------------------------------------------------------------------
EXTRA_COLUMNS: tuple[Column, ...] = (
    Column("others1", "用途コード", "印刷範囲外", 90, "text", outside_print=True),
    Column("others2", "用途名", "印刷範囲外", 130, "text", outside_print=True),
    Column("others3", "納入先", "印刷範囲外", 200, "text", outside_print=True),
    Column("others4", "包装仕様書No", "印刷範囲外", 130, "text", outside_print=True),
    Column("others5", "コイル縦割", "印刷範囲外", 90, "text", outside_print=True),
    Column("others6", "コイル横縦割", "印刷範囲外", 110, "text", outside_print=True),
)


def csv_header(column: Column) -> str:
    """CSVの見出し1つ。**上下2段の見出しを1行に畳む。**

    画面と紙は見出しが2段(「作業停止①」の下に「記号」)ですが、CSVは
    1行しかありません。下段だけにすると「記号」が3つ並んで見分けが
    付かないので、上段を前に付けます ── `作業停止① 記号`。

    「印刷範囲外」は場所の断りであって欄の名前ではないので付けません。
    """
    if column.group and column.group != "印刷範囲外":
        return f"{column.group} {column.label}"
    return column.label

#: 合紙の選べる値。紙の K列は「有 / 無」の2択
INTERLEAF_CHOICES: tuple[str, ...] = ("", "有", "無")

#: 紙の3行目に刷ってある注意書き。画面にも同じ文言を出す
STOP_TIME_NOTE = "※作業停止時間は5分以上で時間を記入"

#: 紙の見出し
SHEET_TITLE = "梱　包　実　績　日　報　表"


def column_groups(columns: tuple[Column, ...] = COLUMNS) -> tuple[tuple[str, int], ...]:
    """上段の見出しと、その幅(列数)。同じ `group` が続くぶんだけ結合する。

    見出しを2段にするのは紙と同じ理由 ── 「梱包作業時間」の下に
    開始・終了がぶら下がっていないと、時分が4つ並んでいるだけに見える。
    """
    out: list[tuple[str, int]] = []
    for column in columns:
        if out and out[-1][0] == column.group and column.group:
            out[-1] = (column.group, out[-1][1] + 1)
        else:
            out.append((column.group, 1))
    return tuple(out)
