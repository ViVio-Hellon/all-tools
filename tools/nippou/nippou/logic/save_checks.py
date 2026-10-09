"""保存の前に見るもの ── VBA `ExecutePrintProcess` の5つの手続き

**数えかたが2つあります。** VBA の手続きは5つですが、その中身は
7つの見方に分かれます。画面には人が見る側(**7項目**)を出します ──
断られたときに並ぶのは項目のほうなので。

【VBAでは「直に1回」の印刷保存が関門だった】
入力中は好きに打てて、**直の終わりに1回だけ押す印刷保存**のところで
まとめて見ていました:

    Number_Count        梱包数が検入枚数を超えていないか
    内訳チェックRun     停止の記号が、内訳マスタにある記号か
    時間計算_シート版   定時に終わっているか / 1行が直より長くないか /
                        開始と終了が同じでないか
    Last_Confi          最終時間まで入っているか / 休憩は足りているか
    Opetime_Calcul      作業時間の合計が、その直の長さを超えていないか

どれか1つでも引っかかると **保存そのものを止めて**いました
(`UFdaily.Tag = "end"` → `Exit Function`)。自動実行のときだけは
「警告は出すが止めない」に変わります(`isAutoMode`)。

【なぜフォームのチェックだけでは足りないのか】
VBA はフォームで入力チェックを通してからシートへ書いていました。
それでもこの5つを保存時にもう一度走らせていたのは、**シートを直接
書き換えられるから**です。フォームを通らずに入った値は、フォームの
チェックを一度も通っていません。

このツールにシートはありませんが、同じ穴があります ── 保存先は
sqlite3 で、「表を見る/直す」画面からも、他の道具からも直に書けます。
だから**画面の入力ではなく、保存済みの中身を読み直して**ここを通します
(`services/shift_check.py`)。判断はぜんぶこの層で、画面は結果を写すだけ。

【この層の約束】
・ここは**純粋**。DBもマスタも触らず、渡された行と時刻だけで決める
・見つけたものは**全部返す**。1つ目で止めない ── 直すのは人で、
  「あと何か所あるのか」が分からないと何度も往復することになる
・1件ごとに **どこ(ページ・行・欄)** と **どう直すか** を持たせる
"""
from __future__ import annotations

import unicodedata
from dataclasses import dataclass
from typing import Any, Iterable, Optional, Sequence

from ..constants import STOP_FIELD_PAIRS
from ..db.models import DetailRecord
from .numeric import is_numeric, to_float
from .work_time import elapsed_minutes, minutes_text, shift_minutes, stop_minutes

#: 断りの種類。**文言から推し量らない**(`logic/work_time.py` と同じ約束)
PACK_OVER = "pack_over"           # Number_Count
UNKNOWN_STOP = "unknown_stop"     # 内訳チェックRun
SHIFT_END = "shift_end"           # 定時エンドチェック + Last_Confi 前半
SHORT_BREAK = "short_break"       # Last_Confi 後半
OVER_SHIFT_TOTAL = "over_shift_total"   # Opetime_Calcul
OVER_SHIFT_ROW = "over_shift_row"       # 時間計算_シート版
SAME_TIME = "same_time"                 # 時間計算_シート版
NEGATIVE_TIME = "negative_time"         # 時間計算(停止が作業時間を超える)

#: **直の終わりにならないと判断できないもの。**
#:
#: 【なぜ分けるのか】
#: 7項目のうち5つは「いま入っている値が、それだけで間違っている」もの
#: です ── 梱包数が検入枚数を超えている、マスタに無い記号が入っている、
#: 開始と終了が同じ。どれも**打っている途中でも間違いだと言い切れます。**
#:
#: 残る2つは違います。最終時間まで入っているか(`SHIFT_END`)と、休憩が
#: 60分あるか(`SHORT_BREAK`)は、**直が終わっていなければ必ず「足りない」**
#: です。8時に1行目を打った時点で「17時まで入っていません」と断ったら、
#: 誰も1行も保存できません。
#:
#: だから**止める場所を分けます** ── 5つは「保存(確定)」で止め、2つは
#: 直の終わり(「共有へ保存」「直の確定」)で止めます。VBA が5つまとめて
#: 印刷保存で見ていたのは、そこが**直に1度しか押さないボタン**だった
#: からで、ページごとに押すこのツールの「保存」とは別のものです。
#:
#: この2つが `skippable`(「設備移動のため保存」で通せる)のと同じなのは
#: 偶然ではありません。どちらも「直を最後までやれたか」の話です。
AT_SHIFT_END: frozenset[str] = frozenset({SHIFT_END, SHORT_BREAK})

#: 休憩に要る分。VBA は3直もそれ以外も 60 分で見ていた
REQUIRED_BREAK_MINUTES = 60

#: 休憩として数える停止の記号。`作業停止時間内訳_1` の「休憩食事」
REST_STOP_CODE = "0"

#: 日勤の残業終わり。VBA `Last_Confi` が定時の代わりに認めていた2つ
DAY_SHIFT_OVERTIME_ENDS: tuple[str, ...] = ("18:00", "19:00")

#: 停止の記号が入る欄と、その時間の欄(紙の P:Q / R:S / T:U。④⑤は紙に無い)
STOP_PAIRS: tuple[tuple[str, str], ...] = STOP_FIELD_PAIRS


@dataclass(frozen=True)
class Finding:
    """見つけたもの1件。**どこと、どう直すかまで持つ。**

    `skippable` は「設備移動のため保存」で通せるかどうか。VBA で
    `UF印刷.C2.Tag = "休憩チェックスキップ"` が飛ばしていたのは
    `Last_Confi` の2本だけで、他は飛ばせませんでした。
    """

    code: str
    message: str
    how: str = ""
    page: int = 0
    row: int = 0
    field: str = ""
    sound: str = ""
    skippable: bool = False

    @property
    def at_shift_end(self) -> bool:
        """直が終わってからでないと判断できないもの(:data:`AT_SHIFT_END`)。"""
        return self.code in AT_SHIFT_END

    @property
    def where(self) -> str:
        """「2ページ 5行目」。ページも行も無いものは空。"""
        parts = []
        if self.page:
            parts.append(f"{self.page}ページ")
        if self.row:
            parts.append(f"{self.row}行目")
        return " ".join(parts)

    def as_dict(self) -> dict:
        return {"code": self.code, "message": self.message, "how": self.how,
                "page": self.page, "row": self.row, "field": self.field,
                "sound": self.sound, "skippable": self.skippable,
                "where": self.where, "at_shift_end": self.at_shift_end}


def normalize(value: str) -> str:
    """比べるための形にそろえる (VBA ``NormalizeValue``)。

    VBA は `StrConv(vbNarrow)` で**全角を半角へ**寄せてから空白を消して
    いました。Python の `NFKC` は英数字は同じく半角へ寄せますが、
    片仮名は逆に**半角を全角へ**寄せます。**向きは違っても、突き合わせる
    2つを同じ関数に通すかぎり結果は変わりません** ── 記号「ｲ」と「イ」は
    どちらの向きでも同じ1つに落ちます。
    """
    if value is None:
        return ""
    return unicodedata.normalize("NFKC", str(value)).replace(" ", "").replace("　", "").strip()


def _int(text: str) -> int:
    return int(to_float(text)) if is_numeric(text) else 0


def _plain(value: Any) -> int | float:
    """10 進で足した分を、整数なら int・小数なら float で返す。"""
    return int(value) if value == int(value) else float(value)


def _sorted(details: Iterable[DetailRecord]) -> list[DetailRecord]:
    return sorted(details, key=lambda d: (d.page, d.row_no))


def _last_page(details: Sequence[DetailRecord]) -> int:
    return max((d.page for d in details), default=0)


def _filled_rows(details: Sequence[DetailRecord], page: int) -> list[DetailRecord]:
    """そのページの、終了(分)まで入っている行。VBA の `LastRow` 探しと同じ列。"""
    return [d for d in details if d.page == page and (d.sh or "").strip() != ""]


# ======================================================================
# Number_Count ── 梱包数が検入枚数を超えていないか
# ======================================================================
def check_pack_count(details: Sequence[DetailRecord]) -> list[Finding]:
    """port of ``Number_Count``。

    ロット番号ごとに **個装枚数 × 包数** を全ページぶん足し、そのロットの
    **検入枚数**と比べます。超えていたら、そのロットの行を挙げます。

    【空欄のロット番号は上の行を引き継ぐ】
    1つのロットが何行にも分かれるので、2行目から先はロット番号が空です
    (VBA の `Kara` 変数)。検入枚数は**そのロットで最初に出てきた行**の
    値を使います ── 続きの行には入っていません。
    """
    rows = _sorted(details)
    packs: dict[str, float] = {}
    inspected: dict[str, float] = {}
    lot_rows: dict[str, list[DetailRecord]] = {}

    current = ""
    for d in rows:
        lot = (d.lot or "").strip()
        if lot:
            current = lot
            if lot not in inspected:
                inspected[lot] = to_float(d.ken) if is_numeric(d.ken) else 0.0
        if not current:
            continue
        packs[current] = packs.get(current, 0.0) + _int(d.mai) * _int(d.tut)
        lot_rows.setdefault(current, []).append(d)

    out: list[Finding] = []
    for lot, total in packs.items():
        limit = inspected.get(lot)
        # 検入枚数が無いロットは見送る(VBA も `CountDic.Exists` で外す)
        if limit is None or total <= limit:
            continue
        for d in lot_rows[lot]:
            # VBA は検入枚数の入っている行だけ色を付けていた
            if (d.ken or "").strip() == "":
                continue
            out.append(Finding(
                PACK_OVER,
                f"ロット {lot}: 梱包数 {total:g} が検入枚数 {limit:g} を超えています",
                "個装単位の枚数と梱包単位の包数、検入枚数のどれかが違います。"
                "紙と見比べて直してください",
                page=d.page, row=d.row_no, field="KEN", sound="pack_over"))
    return out


# ======================================================================
# 内訳チェックRun ── 停止の記号が内訳マスタにあるか
# ======================================================================
def check_stop_codes(details: Sequence[DetailRecord],
                     known_codes: Iterable[str]) -> list[Finding]:
    """port of ``内訳チェックRun`` / ``CheckAndColorCells_NotExist``。

    停止の記号3つ(紙の P/R/T 列)を、`作業停止時間内訳_1/2/3` の
    **内訳番号**と突き合わせます。無い記号は集計のときに
    「その他」へ落ちて**どこにも数えられません**(`CheckCharType`)。

    **マスタが読めないときは何も言いません。** 一覧が空のまま全部を
    「無い記号」と言うと、マスタに届かない日は保存が丸ごと止まります。
    """
    known = {normalize(c) for c in known_codes if str(c).strip()}
    if not known:
        return []

    out: list[Finding] = []
    for d in _sorted(details):
        for index, (code_field, _) in enumerate(STOP_PAIRS, start=1):
            text = (getattr(d, code_field) or "").strip()
            if not text or normalize(text) in known:
                continue
            out.append(Finding(
                UNKNOWN_STOP,
                f"停止内訳にない記号です: 作業停止{index} 「{text}」",
                "停止の記号は一覧から選んでください。"
                "一覧に無いものは集計のどこにも入りません",
                page=d.page, row=d.row_no, field=code_field.upper(),
                sound="unknown_stop"))
    return out


# ======================================================================
# 時間計算_シート版 ── 定時エンド / 1行の長さ / 同時刻
# ======================================================================
def check_shift_end(details: Sequence[DetailRecord], shift: str,
                    end_time: str) -> list[Finding]:
    """port of ``定時エンドチェック_シート版`` + ``Last_Confi`` の前半。

    **最終ページの、最後に埋まっている行の終了時刻**が、その直の定時と
    同じかを見ます。途中で入力が止まっていれば、その直の実績は
    最後まで残っていないということです。

    【VBA の2本を1本にまとめた理由】
    VBA には似た確認が2つありました ── 最後の行が定時ちょうどか
    (`定時エンドチェック_シート版`)と、最終ページのどこかに定時があるか
    (`Last_Confi`)。前者が先に走って止めるので、**後者が日勤に認めて
    いた残業(18:00 / 19:00)は一度も通りませんでした。** 残業した日勤は
    どうやっても保存できないことになるので、ここでは1本にまとめ、
    日勤の残業は通します。
    """
    if not end_time:
        return []
    last_page = _last_page(details)
    filled = _filled_rows(details, last_page)
    if not filled:
        return []

    last = filled[-1]
    wanted = [end_time]
    if "日勤" in shift or "昼" in shift:
        wanted.extend(DAY_SHIFT_OVERTIME_ENDS)

    actual = f"{_int(last.sz):02d}:{_int(last.sh):02d}"
    if any(actual == f"{_int(t.split(':')[0]):02d}:{_int(t.split(':')[1]):02d}"
           for t in wanted if ":" in t):
        return []

    allowed = " / ".join(wanted)
    return [Finding(
        SHIFT_END,
        f"最終時間まで入力がないのでは？ 最後の行の終了は {actual}、"
        f"{shift}の定時は {allowed} です",
        "直の終わりまで行を埋めてください。"
        "設備移動などで途中までのときは、そのまま保存もできます",
        page=last.page, row=last.row_no, field="SZ", skippable=True)]


def check_row_times(details: Sequence[DetailRecord],
                    limit_minutes: int) -> list[Finding]:
    """port of ``時間計算_シート版`` の行ごとの確認。

    1行の作業時間が**直の規定時間より長い**、あるいは**開始と終了が
    同じ**なら挙げます。入力画面でも同じことを見ています
    (`logic/work_time.compute`)が、そこを通らずに入った値のために
    ここでもう一度見ます。

    【ページの変わり目の行は見送る】
    最終ページでないページの、最後に埋まっている行は次のページへ続きます
    (VBA `IsBridgeRow`)。ここだけは長くても構いません。
    """
    rows = _sorted(details)
    last_page = _last_page(rows)
    bridges = set()
    for page in {d.page for d in rows}:
        if page >= last_page:
            continue
        filled = _filled_rows(rows, page)
        if filled:
            bridges.add((page, filled[-1].row_no))

    out: list[Finding] = []
    for d in rows:
        parts = [(d.kz or "").strip(), (d.kh or "").strip(),
                 (d.sz or "").strip(), (d.sh or "").strip()]
        if not all(parts) or not all(is_numeric(p) for p in parts):
            continue
        minutes = elapsed_minutes(*parts)
        if minutes == 0:
            out.append(Finding(
                SAME_TIME,
                f"{d.row_no}行目の時間入力が正しくありません。"
                "開始時間と終了時間が同じになっています",
                "どちらかの打ち間違いです。紙と見比べて直してください",
                page=d.page, row=d.row_no, field="SZ"))
        elif (limit_minutes and minutes > limit_minutes
              and (d.page, d.row_no) not in bridges):
            out.append(Finding(
                OVER_SHIFT_ROW,
                f"{d.row_no}行目の作業時間 {minutes}分 が、"
                f"直の規定時間 {limit_minutes}分 を超えています",
                "開始時間か終了時間の打ち間違いです。"
                "日をまたぐ3直は、終了が翌朝でも構いません",
                page=d.page, row=d.row_no, field="KZ"))
    return out


def check_negative_time(details: Sequence[DetailRecord]) -> list[Finding]:
    """停止時間が作業時間を超えていないか。

    入力画面でも見ています(`logic/work_time.problems`)が、**そこを
    通らずに入った値**のためにここでも見ます ── 自動保存は打ちかけを
    残すために止めないので、直しきらないまま直が終わると、この形の行が
    手元に残ったまま共有へ出ようとします。

    開始・終了が無くても停止だけ入っている行は、0 から引くのでマイナス
    です(VBA `時間計算` と同じ扱い)。
    """
    out: list[Finding] = []
    for d in _sorted(details):
        stops = [(getattr(d, minutes) or "").strip() for _code, minutes in STOP_PAIRS]
        if not any(stops):
            continue
        parts = [(d.kz or "").strip(), (d.kh or "").strip(),
                 (d.sz or "").strip(), (d.sh or "").strip()]
        minutes = 0
        if all(parts) and all(is_numeric(p) for p in parts):
            minutes = elapsed_minutes(*parts)
        if minutes - sum(stop_minutes(text) for text in stops) < 0:
            out.append(Finding(
                NEGATIVE_TIME,
                f"{d.row_no}行目：作業時間がマイナスです",
                "停止時間が作業時間を超えていないか確かめてください",
                page=d.page, row=d.row_no, field="TH",
                sound="negative_time"))
    return out


# ======================================================================
# Last_Confi 後半 ── 休憩は足りているか
# ======================================================================
def break_minutes(details: Sequence[DetailRecord]) -> int | float:
    """休憩として入っている分の合計。停止の記号が「0」のものだけ。

    小数の分も切り捨てずに足します(30.5 + 29.5 を 59 分と数えて断っていた)。
    """
    total = sum(stop_minutes(getattr(d, minutes_field))
                for d in details
                for code_field, minutes_field in STOP_PAIRS
                if normalize(getattr(d, code_field) or "") == REST_STOP_CODE)
    return _plain(total)


def is_all_stop_shift(details: Sequence[DetailRecord]) -> bool:
    """その直が**全停**(直まるごと設備停止)か。

    全停入力は1ページの1行目だけに「直の開始〜終了・停止の記号」を書き、
    ロットは書きません(`logic/pages.is_all_stop_row` と同じ目印)。
    ここで判定を持たないと、`logic` から `logic` を跨いで呼ぶことになります。
    """
    for d in details:
        if str(d.lot or "").strip():
            return False                          # 打った行がある = 全停ではない
    return any(
        all(str(v or "").strip() for v in (d.kz, d.kh, d.sz, d.sh))
        and str(d.s or "").strip()
        for d in details)


def check_break(details: Sequence[DetailRecord], *, day_work: bool,
                required: int = REQUIRED_BREAK_MINUTES) -> list[Finding]:
    """port of ``Last_Confi`` の休憩確認。

    停止の記号が「0」(休憩食事)の分を全ページぶん足して、`required` 分に
    届いているかを見ます。**昼稼働(`day_work`)のときは見ません** ──
    昼を通して動かした直には、そもそも定時の休憩がありません
    (VBA `UFdaily.HIRU`)。

    【全停の直も見ません】
    直まるごと設備が止まっていた日に「休憩が60分足りません」と断るのは
    筋が通りません ── **止まっているのだから、休憩も何もありません。**
    全停入力は1行目に直の開始〜終了をそのまま書くので、そこから休憩を
    60分引く相手もいません。
    """
    if day_work or is_all_stop_shift(details):
        return []
    total = break_minutes(details)
    if total >= required:
        return []
    return [Finding(
        SHORT_BREAK,
        f"{minutes_text(total)}分しか休憩の入力がありません。{required}分必要です",
        "休憩は停止の記号「0」で入れます。"
        "設備移動などで本当に取れていないときは、そのまま保存もできます",
        field="S", skippable=True)]


# ======================================================================
# Opetime_Calcul ── 作業時間の合計が直の長さを超えていないか
# ======================================================================
def check_total_work(details: Sequence[DetailRecord], shift: str,
                     available_minutes: int) -> list[Finding]:
    """port of ``Opetime_Calcul``。

    作業時間(分)を全ページぶん足して、その直の長さ(終わり − 始まり)と
    比べます。超えているなら、どこかの行の時刻が違うか、他の直の行が
    紛れ込んでいます。
    """
    if available_minutes <= 0:
        return []
    # 作業時間は停止を小数のまま引いた数(57.5 など)。切り捨てて足さない
    total = sum(stop_minutes(d.tim) for d in details)
    if total <= available_minutes:
        return []
    return [Finding(
        OVER_SHIFT_TOTAL,
        f"{shift}の作業可能時間（{available_minutes}分）を超えています。"
        f"合計 {minutes_text(total)}分 / 超過 {minutes_text(total - available_minutes)}分",
        _over_shift_how(details),
        field="TIM")]


def _over_shift_how(details: Sequence[DetailRecord]) -> str:
    """**どこが効いているのかを出す。**

    【合計だけ出しても、探しようがありません】

        保存できません。3直の作業可能時間（490分）を超えています。
        合計 1250分 / 超過 760分
        こえてねぇけど

    見ているのは**その直ぜんぶ**(全ページ)です。いま開いているページが
    正しくても、**別のページに残っている行**で超えます。画面に出ている
    紙だけを見ているかぎり、いくら数えても合いません。

    そこで、ページごとの合計と、長い行を上から並べます ── 目の前の
    ページの話なのか、別のページの話なのかが、読めば分かります。
    """
    per_page: dict[int, Any] = {}
    rows: list[tuple[Any, int, int]] = []
    for d in details:
        minutes = stop_minutes(d.tim)
        if minutes <= 0:
            continue
        per_page[d.page] = per_page.get(d.page, 0) + minutes
        rows.append((minutes, d.page, d.row_no))

    if not per_page:
        return ("行のどれかで開始・終了が違っています。"
                "別の直の行が紛れていないかも見てください")

    pages = "、".join(f"{page}ページ {minutes_text(minutes)}分"
                     for page, minutes in sorted(per_page.items()))
    longest = "、".join(f"{page}ページ {row}行目 {minutes_text(minutes)}分"
                       for minutes, page, row in sorted(rows, reverse=True)[:3])
    # **画面にそのまま出る字です。** 飾り(アスタリスク)は書きません
    head = f"ページごとの合計は {pages} です"
    if len(per_page) > 1:
        head += ("。いま開いているページだけでは合いません ── "
                 "ほかのページも見てください")
    return (f"{head}。長いほうから {longest}。"
            "開始・終了が違っていないか、別の直の行が紛れていないかを見てください")


# ======================================================================
# まとめて走らせる
# ======================================================================
def run_all(details: Sequence[DetailRecord], *, shift: str,
            shift_start: str = "", shift_end: str = "",
            limit_minutes: int = 0,
            known_stop_codes: Iterable[str] = (),
            day_work: bool = False,
            required_break: int = REQUIRED_BREAK_MINUTES) -> list[Finding]:
    """7項目ぜんぶ。**順番は VBA と同じ**(見つけた順に上から出る)。

    VBA の手続きは5つで、その中身がこの7つに分かれます
    (時間計算_シート版 が3つ、Last_Confi が2つ)。

    VBA は1本目で止めていましたが、ここは**全部返します**。止める/
    止めないを決めるのは呼び手(`services/shift_check.py`)です。
    """
    if not details:
        return []
    # その直の長さ(分)。VBA `Opetime_Calcul` の `DateDiff` と同じ
    available = shift_minutes(shift_start, shift_end)
    findings: list[Finding] = []
    findings += check_pack_count(details)
    findings += check_stop_codes(details, known_stop_codes)
    findings += check_row_times(details, limit_minutes)
    findings += check_negative_time(details)
    findings += check_shift_end(details, shift, shift_end)
    findings += check_break(details, day_work=day_work, required=required_break)
    findings += check_total_work(details, shift, available)
    return findings


def blocking(findings: Sequence[Finding], *, skip: bool = False) -> list[Finding]:
    """止める理由になるもの。

    `skip` は「設備移動のため保存」を通したとき。VBA が
    `休憩チェックスキップ` で飛ばしていたのと同じ範囲だけ外れます。
    """
    return [f for f in findings if not (skip and f.skippable)]


def unskippable(findings: Sequence[Finding]) -> list[Finding]:
    """**「設備移動のため保存」では通せないもの。**

    逃げ道の欄を出す前にここを見ます ── 梱包数や停止記号の間違いが
    混ざっているのに「下に打てば通ります」とだけ出すと、打っても通らず
    「打ったのに駄目だった」で終わります。何が残っているかを一緒に
    並べれば、打つ前に分かります。
    """
    return [f for f in findings if not f.skippable]


def during_input(findings: Sequence[Finding]) -> list[Finding]:
    """**打っている途中でも「いま間違っている」と言い切れるもの。**

    「保存(確定)」の関門はこちらです。直が終わってからでないと決まらない
    2つ(:data:`AT_SHIFT_END`)を外します ── 外さないと、8時に1行目を
    打った時点で「17時まで入っていません」と断られ、**誰も1行も保存
    できません。**

    外した2つが素通りになるわけではありません。「共有へ保存」と
    「直の確定」は :func:`blocking` のほう(7項目ぜんぶ)を通ります。
    """
    return [f for f in findings if not f.at_shift_end]


def summary(findings: Sequence[Finding]) -> str:
    """画面とログに出す1行。"""
    if not findings:
        return "確かめました。直すところはありません"
    kinds = len({f.code for f in findings})
    return f"直すところが {len(findings)}件（{kinds}種類）あります"


def first_sound(findings: Sequence[Finding]) -> Optional[str]:
    """鳴らす音。**1つだけ** ── 何種類も重ねて鳴らしても伝わらない。"""
    for f in findings:
        if f.sound:
            return f.sound
    return None
