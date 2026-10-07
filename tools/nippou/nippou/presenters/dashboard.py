"""集計の一覧表示(ダッシュボード) ── **主役は「並べて見る」ほう**

【VBAは1枚ずつだった】
`graphF` は `MultiPage1` のタブ(集計 / 停止集計)を切り替えて、
**そのとき1枚だけ**グラフを見せる作りでした(`CopyChartAtCellToUserForm`
がグラフをGIFに書き出してフォームへ貼る)。切り替えないと隣が見えない
ので、「今日は重量が出ているのに稼働率が低い」のような**並べて初めて
分かること**が見えません。

【ここでは並べる。切り替えも残す】
主表示を**タイル**にして、数字・棒・ドーナツ・表を1画面に並べます。
そのうえで、1枚を大きく見る道(VBAと同じ「1つずつ」)も残します ──
細かい値を読むときは大きいほうが早いので、どちらも要ります。

【何を出すかはここが決める】
タイルの中身・並び・単位・丸め・「データが無いとき何と言うか」まで、
判断はこの層です。画面(`views/graph.js`)は座標へ写すだけ ── この
決まりは絵の描き方が変わっても動かしません。

【数字は**残した集計から**取る】
明細を毎回数え直すのではなく、直ごとに残してある集計
(`services/summary.py`)を読みます。ひと月ぶん開いても読むのは
数十行ですし、**そのとき保存した数字がそのまま出る**ので、
紙とグラフが食い違いません。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any, Optional

from ..db.repository import NippouRepository
from ..logic import line_target
from ..logic.aggregation import (MINUTES_PER_DAY, ShiftAggregate,
                                 day_rate_pct, stop_kind_label)
from ..logic.numeric import format_fixed
from ..logic.shift import SHIFT_NAMES

#: タイルの種類。画面はこれを見て描き方を決める
KIND_NUMBER = "number"      # 大きな数字1つ
KIND_BAR = "bar"            # 棒
KIND_LINE = "line"          # 折れ線
KIND_DONUT = "donut"        # ドーナツ(内訳)
KIND_TABLE = "table"        # 表
KIND_COMBO = "combo"        # 棒 + 折れ線(**目盛りが左右で別**)

#: ドーナツに出す項目の上限。**多すぎると読めない**ので、
#: これを超えたぶんは「その他」へまとめる
DONUT_LIMIT = 6

#: 何を主役にするか。**画面の切り替えと1対1。**
#:
#: その日 … 直の終わりに見る(既定)
#: 期間   … 月をまとめて見る。上の数字も停止の輪も期間で数え直す
MODE_DAY = "day"
MODE_PERIOD = "period"

# ----------------------------------------------------------------------
# 数字の種類 (`Tile.tone`)
#
# 【なぜ要るか】
# 7つの数字が同じ色で並ぶと、目が滑って**どれも読まない**画面になります
#(「全部同じで気持ち悪い」)。かといって虹色にすると、色が意味を持たなく
# なって同じことです。そこで**量の種類でだけ**分けます ── 読む人の頭の中
# にもともとある区切りと同じところで。
#
# 【判断はここ(サーバ)】
# 「重量は出来高」「停止は注意して見るもの」はデータの意味の話で、絵の
# 描き方ではありません。画面(`views/graph.js` と `graph.html`)は
# `data-tone` を写すだけで、どの数字が何色かを知りません。
#
# 【大きく2つに分ける】
# 現場が頭の中で分けているのは、まず**枚数の話か、時間の話か**です。
# 色もそこで分けます。停止は時間の一部ですが「ここを見る」ものなので
# 別立てにします:
#
#     枚数のなかま   緑   重量・枚数・ロット数・生産性
#     時間のなかま   青   作業時間・稼働時間・稼働率
#       └ 停止       暖色 止まっていた時間。**注意して見る**が責めない
#     目標のなかま   紫   累積枚数と45度線。**対で読むもの**
#
# 色そのものは `tokens.css` の `--qty` / `--dur` / `--stop`。停止を赤に
# しないのは、停止が失敗ではないからです(休憩食事も停止)。
#
# **棒と線は色を分けます。** 一度「その日の枚数(棒)」と「累積枚数(線)」を
# どちらも緑にしましたが、線が棒を横切るところで棒に溶けました。
# 見ている先も違います ── 棒は「その日いくつ出たか」、線は「目標に対して
# どこまで来たか」。だから別のなかまに置きます。
# ----------------------------------------------------------------------
TONE_QTY = "qty"         # 枚数のなかま(重量・枚数・生産性・ロット数)
TONE_DUR = "dur"         # 時間のなかま(稼働時間・稼働率)
TONE_STOP = "stop"       # 停止。時間の一部だが**ここを見る**。責めない
TONE_GOAL = "goal"       # 目標と、それに対する積み上がり(累積枚数・45度線)
TONE_CUMULATIVE = "cumulative"   # 累積の線。**目標の破線と色でも分ける**
TONE_OK = "ok"           # 残っていない(共有へ未保存 0件)
TONE_TODO = "todo"       # まだ残っている(共有へ未保存 1件以上)

# ----------------------------------------------------------------------
# 並べて見分けるための10色 (`tokens.css` の `--cat-1` 〜 `--cat-10`)
#
# 【なぜ要ったか】
# 停止の項目ごとの絵は、項目が属する**3分類の色**で塗っていました。
# ひと目で「計画された停止か、異常か」が読める代わりに、
#
#     0 休憩食事 / 2 人員不足 / 6 教育 / 1 TPM活動・清掃
#
# が**4つとも同じ水色**になります。凡例を1行ずつ目で追わないと、輪の
# どこがどれか分かりません ── 見分けるための絵で見分けられない。
# おまけに「その他」は色が付かず、暗い地では黒い扇になって沈んでいました。
#
# **上から順に配ります。** 輪は長い順に並ぶので、いちばん長い項目が
# `cat-1`、次が `cat-2` … となります。
#
# 【承知のうえの引き換え】
# 長い順なので、**同じ項目が日によって違う色になります。** 以前これを
# 嫌って分類の色にしたのですが、そのぶん見分けが付かなくなりました。
# 1枚の絵の中で見分けられることを取ります ── 凡例が絵のすぐ下にあり、
# 見比べるのは同じ画面の中だけなので。
# ----------------------------------------------------------------------
CAT_TONES = tuple(f"cat-{i}" for i in range(1, 10))   # cat-1 〜 cat-9

#: 「その他」専用。まとめたぶんは主役ではないので、色を主張させない
CAT_REST = "cat-10"

#: **色を使うのはこの4つだけ。** 残り2つ(`ok` / `todo`)は良し悪しの
#: 印で、量の種類ではありません(状態の色を使う)。
QUANTITY_TONES = (TONE_QTY, TONE_DUR, TONE_STOP, TONE_GOAL)

#: 直の色。**Excelのグラフと同じ 青・赤・緑・紫**
#: (`tokens.css` の `--shift-*`)。**入れ替え禁止**。
#:
#: 【「同じ分類の中は色ではなく形で分ける」をやめました】  [v3.51.0]
#: > 同じカテゴリは似た色のルールは撤廃！
#: > カテゴリ一緒でも分けてなきゃ意味ないでしょ
#: > エクセルグラフの配色のようにやってよ
#:
#: 積み上げた段も、直別の棒も、**並べて見分けるもの**です ── 見分け
#: られなければ、分類が揃っていることに意味がありません。この規則で
#: 2回失敗しました(緑の濃淡4段 / 10色を上から = indigo→cyan→teal)。
#:
#: **表でも帯でもグラフでも同じ色**にします。表で1直が青、グラフで
#: 1直が別の色、では結び付きません ── 直の色はここ1つで決めます。
SHIFT_CLASSES = {
    "1直": "shift-1", "2直": "shift-2", "3直": "shift-3", "日勤": "shift-day",
}

#: 積み上げた段の色。**直の色そのもの**です ── 別に持つと、表の1直と
#: グラフの1直が違う色になります(`SHIFT_CLASSES` の説明)。
SHIFT_QTY_CLASSES = SHIFT_CLASSES

#: 積み上げる順。**下から 1直 → 2直 → 3直 → 日勤**(色も濃い順)。
#: 紙に出る順と同じなので `logic/shift.SHIFT_NAMES` から取ります
SHIFT_STACK_ORDER = SHIFT_NAMES


@dataclass
class Series:
    """複合グラフの1本。**単位が違うものを1枚に重ねるためのもの。**

    直枚数(数百)と累積枚数(数万)は桁が2つ違うので、同じ目盛りに
    乗せると片方が床に貼り付いて読めません。`axis` で左右を分けます。
    """

    key: str
    title: str
    kind: str                            # bar / line
    unit: str = ""
    axis: str = "left"                   # left / right
    #: 同じ名前を持つ棒どうしを**積み上げます**(空なら積まない)。
    #: 目盛りの頭も積んだ高さで決まります(`chart.js`)
    stack: str = ""
    values: list[float] = field(default_factory=list)
    #: なかまの印(`TONE_*`)。**色名ではない** ── 何色かを決めるのは
    #: `tokens.css` で、ここは意味を渡すだけ。
    #:
    #: 1枚に重ねる2本には**別のなかま**を当てます(棒=枚数 / 線=目標)。
    #: 同じ色にすると、線が棒を横切るところで棒に溶けます
    mark: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {"key": self.key, "title": self.title, "kind": self.kind,
                "unit": self.unit, "axis": self.axis, "values": self.values,
                "stack": self.stack, "mark": self.mark}


@dataclass
class Tile:
    """タイル1枚。**画面はこれを写すだけ。**"""

    key: str
    title: str
    kind: str
    note: str = ""                       # 副題(いつのぶんか)
    unit: str = ""
    value: str = ""                      # 数字のタイル
    labels: list[str] = field(default_factory=list)
    values: list[float] = field(default_factory=list)
    series: list[Series] = field(default_factory=list)   # 複合グラフ
    # 45度線(目標値)。**実績とは別枠で持つ** ── 実績の系列に混ぜると
    # 合計や「データがあるか」の判定に目標が混ざってしまう
    target: list[float] = field(default_factory=list)
    target_title: str = "目標値"
    slices: list[dict[str, Any]] = field(default_factory=list)
    columns: list[str] = field(default_factory=list)
    rows: list[list[str]] = field(default_factory=list)
    total: str = ""                      # ドーナツの真ん中
    link: str = ""                       # 「詳しく見る」の行き先
    link_text: str = ""
    empty: str = "この期間のデータはありません"
    #: 数字の種類(`TONE_*`)。**色ではなく意味**を渡す ── 画面は
    #: これを `data-tone` に写すだけで、何色かは知らない
    tone: str = ""
    #: 点1つずつの印(棒グラフ用)。直別の棒を**直の色**で塗るために使う。
    #: 空なら全部同じ色。ここで渡すのは色名ではなく class 名で、色は
    #: `tokens.css` が決める
    point_classes: list[str] = field(default_factory=list)
    #: 計算の途中式(`logic/formula.py`)。**足し算だけの数字には付けません**
    #: ── 「合計重量」に式を出しても読むものがありません。稼働率のように
    #: 割り算・引き算が入るものだけ、どう出したのかを見せます
    steps: list[dict[str, Any]] = field(default_factory=list)

    @property
    def has_data(self) -> bool:
        if self.kind == KIND_NUMBER:
            return self.value != ""
        if self.kind == KIND_DONUT:
            return bool(self.slices)
        if self.kind == KIND_TABLE:
            return bool(self.rows)
        if self.kind == KIND_COMBO:
            return any(any(v for v in s.values) for s in self.series)
        return any(v for v in self.values)

    def as_dict(self) -> dict[str, Any]:
        return {
            "key": self.key, "title": self.title, "kind": self.kind,
            "note": self.note, "unit": self.unit, "value": self.value,
            "labels": self.labels, "values": self.values,
            "series": [s.as_dict() for s in self.series],
            "target": self.target, "target_title": self.target_title,
            "slices": self.slices, "columns": self.columns, "rows": self.rows,
            "total": self.total, "link": self.link, "link_text": self.link_text,
            "empty": self.empty, "has_data": self.has_data,
            "tone": self.tone, "point_classes": self.point_classes,
            "steps": self.steps,
        }


def _sum(rows: list[ShiftAggregate], attr: str) -> float:
    return sum(getattr(r, attr) for r in rows)


#: その日の稼働率。式は `logic/aggregation.py` に1つだけ置いてある
_rate = day_rate_pct


#: まとめたぶんの呼び名。**色も専用**(`CAT_REST`)
REST_LABEL = "その他"


def _donut_slices(pairs: list[tuple[str, float]],
                  marks: Optional[dict[str, str]] = None
                  ) -> tuple[list[dict[str, Any]], float]:
    """内訳を、割合つきの並びにする。**多いぶんは「その他」へ。**

    `marks` は「この項目はこの色」と決まっているときに渡します
    (停止の3分類のように、色そのものに意味がある絵)。

    **渡さなければ、上から順に10色を配ります**(`CAT_TONES`)。以前は
    渡さないと色が付かず、そのタイルのなかまの色1色で塗られていました
    ── 4つも5つも同じ色の扇が並び、凡例を1行ずつ目で追わないと輪の
    どこがどれか分かりません。「その他」は色が付かないままで、暗い地
    では**黒い扇**になって沈んでいました。
    """
    kept = [(label, value) for label, value in pairs if value > 0]
    kept.sort(key=lambda p: -p[1])
    if len(kept) > DONUT_LIMIT:
        rest = sum(v for _, v in kept[DONUT_LIMIT:])
        kept = kept[:DONUT_LIMIT] + [(REST_LABEL, rest)]
    total = sum(v for _, v in kept)
    out = []
    for index, (label, value) in enumerate(kept):
        if marks:
            mark = marks.get(label, "")
        elif label == REST_LABEL:
            mark = CAT_REST
        else:
            # 10色を上から。**足りなくなったら頭へ戻す** ── 戻るのは
            # `DONUT_LIMIT` より多い場合だけで、いまは起きません
            mark = CAT_TONES[index % len(CAT_TONES)]
        out.append({
            "label": label,
            "value": round(value, 1),
            "pct": round(value / total * 100, 1) if total else 0.0,
            "mark": mark,
        })
    return out, total


# ======================================================================
# タイルを1枚ずつ
# ======================================================================
def _number(key: str, title: str, note: str, value: float, unit: str,
            digits: int = 0, link: str = "", link_text: str = "",
            tone: str = "", steps=None) -> Tile:
    return Tile(key=key, title=title, kind=KIND_NUMBER, note=note, unit=unit,
                value=format_fixed(value, digits), link=link,
                link_text=link_text, empty="まだ保存がありません", tone=tone,
                steps=[s.as_dict() for s in (steps or [])])


def today_tiles(rows: list[ShiftAggregate], report_date: str,
                cumulative: float = 0.0, period_note: str = "") -> list[Tile]:
    """その日の数字。VBA の集計シートが帯に出していた7項目。

        直重量(t) / 直枚数(枚) / 停止(分) / 稼働時間(分) /
        生産性(t/h) / 稼働率(%) / 累積枚数(枚)

    **上に並べる3つ**(重量・枚数・稼働率)は、直の終わりに真っ先に
    見るものなので先頭に置きます。累積枚数だけは1日の数ではなく
    **期間の積み上げ**なので、副題にその期間を書きます。

    【色は「枚数の話か、時間の話か」で分ける】
    7つ同じ色で並ぶと目が滑って**どれも読まない**画面になるので、
    枚数のなかま(緑) / 時間のなかま(青) / 停止(暖色)の3つに分けます
    (`TONE_*`)。下の絵も同じ色になるので、「本日の合計重量」と
    「日別の重量推移」が同じものの話だと目で繋がります。

    分けるのは種類までで、**良し悪しは言いません** ── 稼働率が何%なら
    よいかはラインごとに違い、このツールは知らないからです。

    【計算のあるものには途中式を添える】
    稼働率90.0%と出ていても、**その数を作った式はどこにも出ていません**
    でした。疑ったときに確かめられないと、数字そのものが使われなく
    なります。割り算・引き算の入るものには `steps` を添えます
    (`logic/formula.py`)── 足すだけの合計重量には付けません。
    """
    from ..logic import formula

    note = f"{report_date}"
    total = _sum(rows, "weight_ton")
    stop = _sum(rows, "total_stop_minutes")
    operating = _sum(rows, "operating_minutes")
    hours = operating / 60
    steps = {s.key: s for s in formula.day_steps(rows, cumulative,
                                                 period_note or note)}

    def trace(*keys: str):
        """その数を作るのに通った式だけを、**通った順に**添える。"""
        return [steps[k] for k in keys if k in steps]

    return [
        _number("today_weight", "本日の合計重量", note, total, "t", 2,
                link="/records", link_text="紙を見る", tone=TONE_QTY),
        _number("today_count", "本日の合計枚数", note,
                _sum(rows, "sheet_count"), "枚", tone=TONE_QTY),
        _number("today_rate", "本日の稼働率", note, _rate(rows), "%", 1,
                tone=TONE_DUR,
                steps=trace("day_shifts", "day_whole", "day_stop", "day_rate")),
        _number("today_stop", "本日の停止時間", note, stop, "分",
                tone=TONE_STOP, steps=trace("day_stop")),
        _number("today_operating", "本日の稼働時間", note, operating, "分",
                tone=TONE_DUR,
                steps=trace("day_whole", "day_stop", "day_operating")),
        _number("today_productivity", "本日の生産性", note,
                total / hours if hours > 0 else 0.0, "t/h", 2,
                tone=TONE_QTY,
                steps=trace("day_operating", "day_productivity")),
        # 累積枚数は**下の複合グラフの累積の線と同じ色**にして繋げます。
        #
        # ここは `TONE_GOAL` でした。累積と45度線は「対で読むもの」
        # なので同じなかまに置く、という理屈です。ですが v3.44.0 で
        # **線のほうだけ**を `TONE_CUMULATIVE` に分けたので、タイルと
        # 線が別の色になっていました ── 同じ「累積枚数」なのに。
        #
        # 45度線が赤になって(v3.49.0)、その食い違いが目に出ました。
        # **赤は45度線1本の色**なので、タイルは線のほうへ揃えます。
        _number("cumulative_count", "累積枚数", period_note or note,
                cumulative, "枚",
                link="/agg", link_text="ロット別を見る", tone=TONE_CUMULATIVE,
                steps=trace("day_cumulative")),
    ]


def period_tiles(rows: list[ShiftAggregate], note: str) -> list[Tile]:
    """期間の数字。**本日系の7項目を、そのまま期間で数え直したもの。**

        期間集計してしまうと累計枚数しか表示しないのがさみしい
        期間表示は本日系の表示を切り替えてでも
        期間でしか見れないデータを見せるべき

    そのとおりでした。期間を選ぶと、上の数字は「本日」のままで、
    期間のものは累積枚数1つだけ ── 1か月を選んでも、**1か月ぶんの
    稼働率も停止時間も出ていません**でした。

    数え方は日のものと**1つも変えません**(`logic/formula.period_steps`)
    ── 変えると、同じ「稼働率」が日と期間で別のものになります。
    違うのは数える範囲だけです:

        期間合計重量   … 期間の重量をぜんぶ足す
        期間合計枚数   … 期間の枚数をぜんぶ足す(累積枚数と同じ数)
        期間稼働率     … 稼働時間合計 ÷ 予定総時間(1440分 × 直数)
        期間停止時間   … 日ごとの停止の合計
        期間稼働時間   … 予定総時間 − 停止合計
        期間生産性     … 期間合計重量 ÷ 期間稼働時間

    色は本日系と同じ(枚数のなかま / 時間のなかま / 停止)。下の推移の
    絵と同じ色なので、上の数字と下の絵が同じものの話だと目で繋がります。
    """
    from ..logic import formula

    total = _sum(rows, "weight_ton")
    stop = _sum(rows, "total_stop_minutes")
    whole = float(MINUTES_PER_DAY * len(rows))
    operating = whole - stop
    hours = operating / 60
    rate = max(0.0, operating / whole * 100) if whole > 0 else 0.0
    steps = {s.key: s for s in formula.period_steps(rows, note)}

    def trace(*keys: str):
        return [steps[k] for k in keys if k in steps]

    return [
        _number("period_weight", "期間の合計重量", note, total, "t", 2,
                link="/agg", link_text="表で見る", tone=TONE_QTY),
        _number("period_count", "期間の合計枚数", note,
                _sum(rows, "sheet_count"), "枚", tone=TONE_CUMULATIVE,
                link="/agg", link_text="ロット別を見る"),
        _number("period_rate", "期間の稼働率", note, rate, "%", 1,
                tone=TONE_DUR,
                steps=trace("period_shifts", "period_whole", "period_stop",
                            "period_operating", "period_rate")),
        _number("period_stop", "期間の停止時間", note, stop, "分",
                tone=TONE_STOP, steps=trace("period_stop")),
        _number("period_operating", "期間の稼働時間", note, operating, "分",
                tone=TONE_DUR,
                steps=trace("period_whole", "period_stop",
                            "period_operating")),
        _number("period_productivity", "期間の生産性", note,
                total / hours if hours > 0 else 0.0, "t/h", 2,
                tone=TONE_QTY,
                steps=trace("period_operating", "period_productivity")),
    ]


def pending_tile(count: int) -> Tile:
    """共有へまだ出していないページ。**0でも出す。**

    0のときに消えると、「押し忘れているのか、そもそも無いのか」が
    分かりません。0は0と書きます。

    **ここだけは良し悪しを言います。** 0件は「残っていない」、1件以上は
    「まだやることがある」で、意味に迷いがありません(稼働率と違って
    ラインごとの基準もありません)。色で先に分かると、数字を読む前に
    片付いているかどうかが伝わります。
    """
    return Tile(
        key="pending", title="共有へ未保存", kind=KIND_NUMBER,
        note="手元には保存済み。まだ共有へ書いていないページ",
        unit="件", value=str(count),
        tone=TONE_TODO if count else TONE_OK,
        link="/settings", link_text="共有へ保存する画面へ")


def shift_tiles(rows: list[ShiftAggregate], scope: str = "本日") -> list[Tile]:
    """直ごとの棒。**枚数と重量を分けて出す** ── 単位が違うものを1枚に
    重ねると、小さいほうが潰れて読めません。

    【棒は直の色で塗ります】                              [v3.51.0]
    一度は「どの棒がどの直かは横軸に書いてあるのだから、色でも言うのは
    意味のない多色使い」として1色にしていました。**やめました。**

    > 同じカテゴリは似た色のルールは撤廃！
    > カテゴリ一緒でも分けてなきゃ意味ないでしょ

    上の複合グラフで直を色で積むので、ここだけ1色だと**同じ画面の中で
    1直の色が2通り**になります。直の色は `SHIFT_CLASSES` ただ1つです。
    """
    labels = [r.shift for r in rows]
    marks = [SHIFT_CLASSES.get(r.shift, "") for r in rows]
    return [
        Tile(key="shift_weight", title="直別の重量", kind=KIND_BAR,
             note=scope, unit="t", labels=labels, tone=TONE_QTY,
             values=[round(r.weight_ton, 3) for r in rows],
             point_classes=marks,
             # 直はあるが重量が0(単重が無い・重量を打っていない)のときに
             # 「保存データはまだありません」と言わない
             empty=(f"{scope}の直({'・'.join(labels)})は重量が0です(単重・重量が未入力)"
                    if rows else f"{scope}の保存データはまだありません")),
        Tile(key="shift_stop", title="直別の停止時間", kind=KIND_BAR,
             note=scope, unit="分", labels=labels, tone=TONE_STOP,
             values=[round(r.total_stop_minutes, 1) for r in rows],
             point_classes=marks,
             empty=(f"{scope}の直({'・'.join(labels)})は停止が0分です"
                    if rows else f"{scope}の保存データはまだありません")),
    ]


#: 停止の3分類の色。**入れ替え禁止**(`tokens.css` の `--stop-*`)。
#
# ここを8色の回し使いに任せていたので、長い順に並べ替えた
# 拍子に突発停止が管理ロスと同じ緑で出ていました ── 3分類は「この色は
# これ」と決まっているものなので、並び順ではなく**項目で**決めます。
STOP_KIND_MARKS = {
    "管理ロス停止": "stop-loss",
    "突発停止": "stop-sudden",
    "ハンドリング停止": "stop-handling",
}


def stop_kind_tile(rows: list[ShiftAggregate], scope: str = "本日") -> Tile:
    """停止の3分類。VBA `Stop_Distr` の振り分けそのもの。

    **名前は溜めてある文字ではなく、現場の呼び名で出します**
    (`aggregation.STOP_KIND_LABELS`)── 色を決める `STOP_KIND_MARKS`
    の鍵は溜めてある文字のままなので、そちらで引いてから言い換えます。
    """
    slices, total = _donut_slices([
        (stop_kind_label("管理ロス停止"), _sum(rows, "management_loss_minutes")),
        (stop_kind_label("突発停止"), _sum(rows, "unplanned_stop_minutes")),
        (stop_kind_label("ハンドリング停止"),
         _sum(rows, "handling_stop_minutes")),
    ], {stop_kind_label(k): v for k, v in STOP_KIND_MARKS.items()})
    return Tile(key="stop_kinds", title="停止の内訳(分類)", kind=KIND_DONUT,
                note=scope, unit="分", slices=slices,
                total=format_fixed(total, 0),
                empty=f"{scope}の停止入力はありません")


def stop_item_tile(items, scope: str = "本日") -> Tile:
    """停止の項目ごと(VBA `Stop_Agg` → `停止グラフ挿入` の円グラフ)。

    `items` は `(停止, 回数)` の並び(`services/summary.stop_items`)。

    【色は**項目ごと**に配ります】
    ここは一度、項目が属する3分類の色で塗っていました。ひと目で
    「計画された停止か、異常か」が読める代わりに、

        0 休憩食事 / 2 人員不足 / 6 教育 / 1 TPM活動・清掃

    が**4つとも同じ水色**になります ── 凡例を1行ずつ目で追わないと、
    輪のどこがどれか分かりません。見分けるための絵で見分けられない。

    いまは指定の10色を**上から順に**配ります(`_donut_slices`)。長い順に
    並ぶので、**同じ項目が日によって違う色になります** ── それは承知
    のうえで、1枚の絵の中で見分けられるほうを取りました。凡例は絵の
    すぐ下にあり、見比べるのは同じ画面の中だけなので。

    分類のほうは、すぐ上の「停止の内訳(分類)」が3色で受け持ちます ──
    **どちらの読み方も、別々の絵で残ります。**
    """
    slices, total = _donut_slices(
        [(s.text, s.stop_minutes) for s, _ in items])
    return Tile(key="stop_items", title="停止の項目ごと", kind=KIND_DONUT,
                note=f"{scope} / 長い順", unit="分", slices=slices,
                total=format_fixed(total, 0),
                empty=f"{scope}の停止入力はありません")


def history_tiles(history: list[ShiftAggregate], note: str) -> list[Tile]:
    """期間の推移。VBA の集計シートは常に月初〜出力時までを描いていた。

    線の色は**上の数字と同じ種類の色**にします ── 「本日の合計重量」と
    「日別の重量推移」が同じ緑なら、上の数字と下の絵が同じものの話だと
    目で繋がります。
    """
    labels = [r.report_date for r in history]
    return [
        Tile(key="trend_weight", title="日別の重量推移", kind=KIND_LINE,
             note=note, unit="t", labels=labels, tone=TONE_QTY,
             values=[round(r.weight_ton, 3) for r in history]),
        Tile(key="trend_rate", title="日別の稼働率推移", kind=KIND_LINE,
             note=note, unit="%", labels=labels, tone=TONE_DUR,
             values=[round(r.operating_rate_pct, 1) for r in history]),
        Tile(key="trend_count", title="日別の枚数推移", kind=KIND_LINE,
             note=note, unit="枚", labels=labels, tone=TONE_QTY,
             values=[round(r.sheet_count, 1) for r in history]),
    ]


def shift_table_tile(rows: list[ShiftAggregate], scope: str = "本日") -> Tile:
    return Tile(
        key="shift_table", title=f"{scope}の直別集計", kind=KIND_TABLE,
        note=scope,
        columns=["直", "枚数", "重量(t)", "作業(分)", "管理ロス", "突発",
                 "ﾊﾝﾄﾞﾘﾝｸﾞ", "稼働率(%)", "生産性(t/h)"],
        rows=[[r.shift,
               format_fixed(r.sheet_count, 0),
               format_fixed(r.weight_ton, 2),
               format_fixed(r.work_minutes, 0),
               format_fixed(r.management_loss_minutes, 0),
               format_fixed(r.unplanned_stop_minutes, 0),
               format_fixed(r.handling_stop_minutes, 0),
               format_fixed(r.operating_rate_pct, 1),
               format_fixed(r.productivity_t_per_h, 2)] for r in rows],
        empty=f"{scope}の保存データはまだありません")


def stop_table_tile(items, scope: str = "本日") -> Tile:
    return Tile(
        key="stop_table", title="停止の項目(表)", kind=KIND_TABLE,
        note=f"{scope} / 長い順",
        columns=["記号・内訳", "分類", "回数", "時間(分)"],
        rows=[[stop.text, stop.stop_kind, str(times),
               format_fixed(stop.stop_minutes, 0)] for stop, times in items],
        empty=f"{scope}の停止入力はありません")


def cumulative_tile(points, note: str) -> Tile:
    """直枚数と累積枚数の推移。**VBAの集計シートの主役だった絵。**

    桁が2つ違うので、棒(その日の枚数)は左、折れ線(累積)は右の目盛りに
    分けます ── 同じ目盛りに乗せると棒が床に貼り付いて読めません。

    **棒と線は色を分けます。** 一度どちらも枚数の緑にしましたが、線が
    棒を横切るところで棒に溶けました。見ている先も違います ── 棒は
    「その日いくつ出たか」、線は「目標に対してどこまで来たか」。

    【累積の線と目標の線も、色で分けます】
    この2本も一度は同じ紫にして、実線/破線だけで分けていました。
    「対で読むものだから同じなかま」という理屈は通っていますが、
    **線が2本重なる絵で、線種だけを頼りにするのは細すぎます** ──
    点が詰まると実線も破線も同じ帯に見え、どちらが目標か分からなく
    なります。累積は teal、目標は赤の破線。**色でも線種でも分かれます。**

    【棒は直ごとに積み上げます ── VBA と同じ】      [v3.50.0]
    > 棒グラフが積み上げ棒グラフとして扱われてます？
    > 直枚数で積み上げならおかしくないでしょ？VBAではそうなってるが

    積み上げていませんでした。棒は1本で、値は**その日の全直合計** ──
    3直ぶんが入っているのに1本に潰れていて、題の「直枚数」とも
    食い違っていました(凡例だけが「その日の枚数」と正しかった)。

    下から 1直 → 2直 → 3直 → 日勤 の順に積みます。**棒の高さ(その日の
    合計)は今までと同じ**で、中の切れ目が増えるだけです ── 45度線に
    対するペースの読み方は変わりません。

    段の色は**枚数の緑の濃淡4段**(`SHIFT_QTY_CLASSES`)。直の青を
    持ってくると、枚数の絵に時間の色が差します。
    """
    labels = [p.work_date for p in points]
    # **その日に出てこない直は、段そのものを作りません。** 日勤だけの
    # ラインで「1直 0枚」の凡例が並ぶと、動いていない直があるように
    # 読めます
    seen = {shift for p in points for shift in p.quantity_by_shift}
    bars = [
        Series(key=f"count_{shift}", title=shift, kind="bar", unit="枚",
               axis="left", stack="day", mark=SHIFT_QTY_CLASSES[shift],
               values=[round(p.quantity_by_shift.get(shift, 0.0), 1)
                       for p in points])
        for shift in SHIFT_STACK_ORDER if shift in seen
    ]
    if not bars:
        # 直の内訳が無い(古い集計を読んだ)。**棒を消しません** ──
        # 合計だけでも絵は成り立ちます
        bars = [Series(key="count", title="その日の枚数", kind="bar",
                       unit="枚", axis="left", mark=TONE_QTY,
                       values=[round(p.quantity, 1) for p in points])]
    return Tile(
        key="trend_cumulative", title="直枚数・累積枚数の推移",
        kind=KIND_COMBO, note=note, unit="枚",
        labels=labels,
        series=[
            *bars,
            Series(key="cumulative", title="累積枚数", kind="line", unit="枚",
                   axis="right", mark=TONE_CUMULATIVE,
                   values=[round(p.cumulative_quantity, 1) for p in points]),
        ])


# ======================================================================
# 45度線(目標値)
# ======================================================================
def add_targets(tiles: list[Tile], target: Optional[float]) -> None:
    """目標線を重ねる (VBA `グラフ挿入` の「★目標値追加」)。

    VBA が引いていたのと同じ形で、同じ2枚に引きます:

        直枚数・累積枚数の推移   累積の側に 目標 × k (45度線。第2軸)
        日別の枚数推移           目標 で横ばい(VBA は 直枚上昇 = 0)

    **目標が無ければ何もしません。** 0 や未設定のラインで水平線を引いても
    読む意味がなく、VBA も `累積目標 <> 0` を見ていました。

    点の数は**タイルごとにそのタイルの目盛りから採ります。** 累積は日別の
    積み上げ、枚数の推移は保存のあった日だけ、と数が違うことがあり、
    片方の数で引くと線が途中で切れたり横へはみ出したりします。
    """
    if not target:
        return

    for tile in tiles:
        points = len(tile.labels)
        if not points:
            continue
        if tile.key == "trend_cumulative":
            # 複合は累積と同じ目盛り(右)に乗せる。左(その日の枚数)へ
            # 乗せると桁が2つ違って床に貼り付く
            #
            # **色は累積と分けます。** 対で読むものですが、線が2本
            # 重なる絵で線種だけを頼りにするのは細すぎます ── 点が
            # 詰まると実線も破線も同じ帯に見え、どちらが目標か分から
            # なくなります。目標は紫の破線、累積は teal の実線
            tile.series.append(Series(
                key="target", title="目標値", kind="line", unit="枚",
                axis="right", mark=TONE_GOAL,
                values=[round(v, 1) for v in
                        line_target.cumulative_line(target, points)]))
        elif tile.key == "trend_count":
            tile.target = [round(v, 1) for v in
                           line_target.flat_line(target, points)]


# ======================================================================
# 1画面ぶん
# ======================================================================
@dataclass
class Dashboard:
    report_date: str
    line: str
    start: str
    end: str
    tiles: list[Tile]
    #: このラインの目標(1日あたりの枚数)。無ければ None
    target: Optional[float] = None
    #: 何を主役にしているか(`MODE_DAY` / `MODE_PERIOD`)。画面の
    #: 切り替えと1対1で、押された側をそのまま返します
    mode: str = MODE_DAY
    #: この数字を引いた時刻("HH:MM")。
    #:
    #: **開きっぱなしにできる画面なので、要る。** グラフは開いたときに
    #: 1度引くだけなので、そのまま置いておくと3時間前の数字が今の顔で
    #: 出ます。いつの数字かを添えれば、見た人が自分で判断できます
    #: (`静的な値に時刻を添える`のは、VBA の集計シートが
    #: 「月初〜出力タイミングまで」だったのと同じ考え方)
    generated_at: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {"report_date": self.report_date, "line": self.line,
                "start": self.start, "end": self.end, "mode": self.mode,
                "tiles": [t.as_dict() for t in self.tiles],
                "target": self.target, "target_text": self.target_text,
                "choices": self.choices, "generated_at": self.generated_at}

    @property
    def target_text(self) -> str:
        """目標を画面の文字に。**枚数に小数点を付けない**(12.0 と出さない)。"""
        if not self.target:
            return ""
        if self.target == int(self.target):
            return f"{int(self.target):,}"
        return f"{self.target:,.1f}"

    def tile(self, key: str) -> Optional[Tile]:
        return next((t for t in self.tiles if t.key == key), None)

    @property
    def choices(self) -> list[dict[str, str]]:
        """「1つずつ大きく見る」の選択肢(VBA `graphF` の MultiPage 相当)。

        **絵になるものだけ。** 数字や表を「大きく見る」に並べても、
        選んだ先に出せるものがありません。
        """
        return [{"key": t.key, "title": t.title} for t in self.tiles
                if t.kind in (KIND_BAR, KIND_LINE, KIND_DONUT, KIND_COMBO)]


def build(repo: NippouRepository, *, report_date: str, line: str,
          start: date, end: date, pending: int = 0,
          target: Optional[float] = None,
          mode: str = MODE_DAY) -> Dashboard:
    """1画面ぶんのタイルを組み立てる。**DBを読むのはここだけ。**

    並びがそのまま画面の並びです ── 上から「今日の数字」「未保存」
    「直ごと」「停止」「推移」「表」。直の終わりに見る順で置いてあります。

    読むのは**残した集計**です(`services/summary.py`)。まだ集計の
    無い直があれば、そこで作ってから読みます ── この仕組みより前に
    保存したぶんを、開いた人が気にせずに済むように。

    停止の内訳名を渡す引数はありません。**名前は保存したときのものが
    一緒に残って**いるので、あとからマスタが差し替わっても、当時の
    グラフは当時の名前のまま出ます。
    """
    from ..services import summary as summary_service

    points = summary_service.day_points(repo, start, end, line)
    history = summary_service.period_rows(repo, start, end, line)
    note = f"{start.isoformat()} 〜 {end.isoformat()}"
    # 累積は**期間の積み上げ**。VBA の集計シートが常に月初〜出力時まで
    # を描いていたので、既定の期間もそこに合わせてあります
    cumulative = points[-1].cumulative_quantity if points else 0.0
    period = mode == MODE_PERIOD

    # **上の数字も、直別も、停止も、まとめて切り替えます。**
    #
    #     停止累計の円グラフ が出ないですね
    #     日集計でしか停止は出せないんですか？ それでは困ります
    #     期間表示は本日系の表示を切り替えてでも
    #     期間でしか見れないデータを見せるべき
    #
    # 期間を選んでいるのに「本日の…」が並び、停止の輪はその日のぶん
    # だけ ── 1か月を選んでも1か月の停止が出ませんでした。出どころを
    # 差し替えるだけで、数え方も絵も同じものを使います。
    if period:
        shifts = summary_service.by_shift(
            summary_service.shift_rows(repo, start, end, line))
        items = summary_service.period_stop_items(repo, start, end, line)
        scope = "期間"
        head = period_tiles(
            summary_service.shift_rows(repo, start, end, line), note)
    else:
        shifts = summary_service.day_rows(repo, report_date, line)
        items = summary_service.day_stop_items(repo, report_date, line)
        scope = "本日"
        head = today_tiles(shifts, report_date, cumulative=cumulative,
                           period_note=note)

    tiles: list[Tile] = []
    tiles += head
    tiles.append(pending_tile(pending))
    tiles += shift_tiles(shifts, scope)
    tiles.append(stop_kind_tile(shifts, scope))
    tiles.append(stop_item_tile(items, scope))
    tiles.append(cumulative_tile(points, note))
    tiles += history_tiles(history, note)
    tiles.append(shift_table_tile(shifts, scope))
    tiles.append(stop_table_tile(items, scope))
    # 45度線。**実績を組み立てたあとに重ねる** ── 目標が無くても
    # グラフは今までどおり出る
    add_targets(tiles, target)
    return Dashboard(report_date=report_date, line=line,
                     start=start.isoformat(), end=end.isoformat(), tiles=tiles,
                     target=target, mode=MODE_PERIOD if period else MODE_DAY,
                     # **引いた時刻はここで押す。** 呼び手に任せると、
                     # 押し忘れた道からは「いつの数字か」が消えます
                     generated_at=datetime.now().strftime("%H:%M"))
