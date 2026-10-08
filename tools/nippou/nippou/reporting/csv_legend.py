"""CSVの列の説明を1枚 ── **どの数がどれか、開いた人が分かるように**

【なぜ要るのか】
「CSVどこがどの数値でとかぱっと見分かるの？」と聞かれました。答えは
**ファイルによります。**

    集計_       14列  … Excel で1画面に収まる。読める
    停止内訳_   14列  … 読める
    計算内容_    8列  … 式が書いてあるので、むしろ一番分かる
    集計明細_   40列  … **見出し行だけで299文字。読めません**

紙(梱包実績日報表)が40欄あるので、紙を写している以上そうなります。
それと、列の名前を読んでも区別が付かない組があります ──
`稼働時間合計` `操業時間` `作業時間合計` は3つとも「時間」ですが、
**引くものが違います。**

そこで説明を1枚置きます。はじめは日ごとのフォルダに毎回入れていましたが、

    日フォルダ内に毎回入れる CSVの読み方、計算内容は年月フォルダに
    1回だけ入れてください 無ければ入れる

**年月のフォルダに1つだけ**にしました(v4.2.0)。説明は日によって変わらない
ので、日の数だけ同じものが並んでいました。式の説明(`計算内容.csv`)も
同じ年月のフォルダに置きます(`logic/formula.guide_rows`)。日のフォルダに
入るのはCSV3本(集計_ / 集計明細_ / 停止内訳_)だけです。

【列の名前はCSVから拾います】
説明の見出しをここに手で並べると、CSVに列が増えたときに**説明だけ
古くなります。** 列の並びは `csv_export` の `FIELDNAMES` などから
そのまま読み、説明はこのファイルの `DESCRIPTIONS` から引きます。
どちらかに漏れがあれば `tests/test_csv_legend.py` が落ちます。

【なぜ .txt なのか】
説明は文章です。CSVにすると、読むためにまたExcelが要ります ──
**メモ帳で開いてそのまま読める**ほうが目的に合っています。
BOM付きUTF-8・CRLF なので、Windowsのメモ帳でそのまま開けます。
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Optional
from unicodedata import east_asian_width

#: 説明のファイル名。**日付を付けません** ── その日の数字ではなく
#: 「読み方」なので、データのファイルと見分けが付くように
FILENAME = "CSVの読み方.txt"

#: 式の説明。**年月のフォルダに1つ**(日付もラインも付けない ── 数の入って
#: いない説明なので、どの日にも当てはまる)
GUIDE_FILENAME = "計算内容.csv"

#: メモ帳の既定の幅に収まる長さ(全角は2つぶんで数えます)
_WIDTH = 76

#: 列名を並べる幅。ここを越える名前は折り返さずにはみ出させます ──
#: 名前を切ると、CSVの見出しと見比べられなくなるので
_NAME_WIDTH = 24

# ======================================================================
# 列の説明
#
# **鍵はCSVの見出しそのもの**です。`日付` のように複数のファイルに
# 出てくる列は1つで足ります(意味が同じなので)。
# ======================================================================
DESCRIPTIONS: dict[str, str] = {
    # -- どの行かを指すもの --------------------------------------------
    "日付": "作業日。直の始まりの日で数えます"
            "(3直は日をまたぐので、始めた日のままです)",
    "ライン": "設備の名前",
    "直": "1直 / 2直 / 3直 / 日勤",
    "ページ": "その直の何枚目の紙か。12行を使い切ると次のページになります"
          "(停止内訳_ では「1件ずつ」の行にだけ入ります)",
    "行": "紙の何行目か(1〜12)。紙のその行をそのまま開けます"
          "(停止内訳_ では「1件ずつ」の行にだけ入ります)",

    # -- 集計_ ---------------------------------------------------------
    "合計枚数": "その直で梱包した枚数。明細の「実績合計 枚数」を足したもの",
    "合計重量(t)": "同じく重量。明細はkg、ここはtです(÷1000)  ★",
    "作業時間合計(分)": "明細の「作業時間(分)」を足したもの。"
                        "人が手を動かしていた時間です  ★",
    "稼働時間合計(分)": "1440分(1日) − 停止合計。設備が動けた時間です  ★",
    "操業時間(分)": "1440分 − 管理ロス設備停止だけ。"
                    "突発とハンドリングは引きません  ★",
    "管理ロス設備停止(分)": "記号が数字の停止。休憩・食事・TPMなど、"
                            "計画された止まりです",
    "段取り・突発停止(分)": "記号がカタカナの停止。機械の突発、"
                            "フォーク待ちなど",
    "ハンドリング停止(分)": "記号が英字の停止。段取り変更、内径カットなど",
    "停止合計(分)": "上の3つの合計",
    "稼働率(%)": "稼働時間合計 ÷ 1440分 × 100",
    "生産性(t/h)": "合計重量(t) ÷ 稼働時間(h)。"
                   "稼働時間が0なら割れないので0と出します",

    # -- 停止内訳_ -----------------------------------------------------
    "区分": "「直の合計」= その記号ぶんをまとめた行。"
            "「1件ずつ」= その合計の中身で、すぐ下に並びます。"
            "合計だけ見るときは「直の合計」で絞ってください",
    "分類": "管理ロス設備停止 / 段取り・突発停止 / ハンドリング停止。"
            "記号の文字種(数字・カタカナ・英字)で決まります",
    "記号": "紙の「作業停止①②③ 記号」(画面では①〜⑤)に書いた記号",
    "内訳": "その記号の名前。マスタが読めたときだけ入ります",
    "LOTNO": "その停止が起きた行のLOTNO(「1件ずつ」の行だけ)",
    "停止の位置": "紙の 作業停止①/②/③ のどれか(「1件ずつ」の行だけ)",
    "回数": "その記号を書いた回数。時間が空でも1回と数えます"
            "(「1件ずつ」の行は必ず1)",
    "停止時間(分)": "その記号ぶんの合計(「1件ずつ」の行はその1件ぶん)",
    "時間(分)": "その記号ぶんの合計(月別のファイルでの呼び方)",
    "その直の停止に占める割合(%)": "その直の停止合計に対する割合。"
                                   "1日ではなく直の中で見ます",

    # -- 計算内容.csv(年月のフォルダ) ---------------------------------
    "対象": "直ごと / その日ぜんぶ。どの単位の数の式か",
    "項目": "どの数の式か(稼働率(%) など)",
    "式": "式の形。その日の答えは 集計_ の同じ名前の列にあります",
    "注記": "式だけでは分からないこと。"
            "VBAと違うところ、間違えやすいところを書いてあります",

    # -- 集計明細_ -----------------------------------------------------
    "係数処理ﾛｯﾄ数": "負荷係数で数えたロット数。"
                     "コイル形状のライン(機側/NS1)だけ入ります",
    "ロット№": "紙のA列",
    "材・調質": "紙のB列",
    "製品寸法 厚×幅×丈": "紙のC:D列",
    "検入枚数": "検査に入ってきた枚数。紙のE列  ★",
    "梱包作業時間 開始 時": "紙のF列。2桁で出します(08)",
    "梱包作業時間 開始 分": "紙のG列",
    "梱包作業時間 終了 時": "紙のH列",
    "梱包作業時間 終了 分": "紙のI列",
    "作業人数": "紙のJ列",
    "合紙": "有 / 無。紙のK列",
    "梱包数量 個装単位 枚数": "1包に何枚入れたか。紙のL列  ★",
    "梱包数量 梱包単位 包数": "何包作ったか。紙のM列",
    "ＶＣ種別 両面・片面": "紙のN列",
    "etc 反転・EX etc": "ｽﾄｱ / 耳付 / 外注 / EX / 反転 / ｽﾎﾟｯﾄ。紙のO列",
    "作業停止① 記号": "紙のP列",
    "作業停止① 時間(分)": "紙のQ列",
    "作業停止② 記号": "紙のR列。①が空でも②のまま出します",
    "作業停止② 時間(分)": "紙のS列",
    "作業停止③ 記号": "紙のT列",
    "作業停止③ 時間(分)": "紙のU列",
    "作業停止④ 記号": "紙には無い欄(画面だけ。v4.24.0)。CSVではいちばん後ろ",
    "作業停止④ 時間(分)": "紙には無い欄(画面だけ。v4.24.0)。作業時間から引く",
    "作業停止⑤ 記号": "紙には無い欄(画面だけ。v4.24.0)。CSVではいちばん後ろ",
    "作業停止⑤ 時間(分)": "紙には無い欄(画面だけ。v4.24.0)。作業時間から引く",
    "実績合計 枚数": "実際に梱包した枚数。紙のV列  ★",
    "実績合計 重量": "実際に梱包した重量(kg)。紙のW列  ★",
    "作業時間(分)": "終了時刻 − 開始時刻 − 停止。紙には載りません(印刷範囲の外)",
    "単重": "1枚あたりの重さ。紙には載りません",
    "用途コード": "紙のAN列。印刷範囲の外なので紙には出ません",
    "用途名": "紙のAO列。同上",
    "納入先": "紙のAP列。同上",
    "包装仕様書No": "紙のAQ列。同上",
    "コイル縦割": "紙のAR列。同上",
    "コイル横縦割": "紙のAS列。同上",
    "作業者": "その直の作業者。直に1つですが、並べ替えても分かるよう"
              "行ごとに入れています",
    "昼稼働": "有 / 無。直に1つ",
    "作業コメント": "紙の「理由」。直に1つ",
    "引当番号": "この端末だけの控え。紙にも共有の日報管理にもありません",
}

#: 間違えやすい組。★の付いた列がここに出てきます。
#: (見出し, ((列名, 違い), …), 締めの一言)
CONFUSABLE: tuple[tuple[str, tuple[tuple[str, str], ...], str], ...] = (
    ("「時間」が3つあります。引くものが違います",
     (("作業時間合計(分)", "明細に打った時間の合計。人が動いていた時間"),
      ("稼働時間合計(分)",
       "1440分 − 停止ぜんぶ(管理ロス + 段取り・突発 + ハンドリング)"),
      ("操業時間(分)", "1440分 − 管理ロス設備停止だけ")),
     "稼働率を出すのに使うのは「稼働時間合計」です。操業時間は使いません。"),
    ("「枚数」が3つあります",
     (("検入枚数", "検査に入ってきた枚数(これから梱包するぶん)"),
      ("梱包数量 個装単位 枚数", "1包に何枚入れたか"),
      ("実績合計 枚数", "実際に梱包した枚数")),
     "集計の「合計枚数」は、この「実績合計 枚数」を足したものです。"),
    ("「重量」は単位が2つあります",
     (("実績合計 重量", "明細の列。kg です"),
      ("合計重量(t)", "集計の列。t です")),
     "t は kg を1000で割ったものです。同じ数を単位だけ変えてあります。"),
)

#: どのCSVにも共通する読み方
COMMON_NOTES: tuple[str, ...] = (
    "空いている欄は「打っていない」です。0ではありません。",
    "時・分は2桁で出します(8時 → 08)。紙と同じ形です。",
    "数字の出どころは、保存のたびに作り直している集計です ── "
    "画面のグラフや集計管理の表と同じ数が出ます。",
    "文字コードはBOM付きUTF-8です。Windows版Excelでそのまま開けます。",
)


# ======================================================================
# 組み立て
# ======================================================================
def _width_of(text: str) -> int:
    """メモ帳での見た目の幅。全角は2つぶんで数えます。"""
    return sum(2 if east_asian_width(ch) in "WFA" else 1 for ch in text)


def _wrap(text: str, width: int) -> list[str]:
    """見た目の幅で折り返す。単語ではなく幅で切ります ──
    日本語には単語の切れ目が無いので。"""
    lines: list[str] = []
    current = ""
    for ch in text:
        if _width_of(current) + _width_of(ch) > width and current:
            lines.append(current)
            current = ""
        current += ch
    if current:
        lines.append(current)
    return lines or [""]


def _pair(name: str, text: str, *, left: int = 2,
          name_width: int = _NAME_WIDTH) -> list[str]:
    """「名前   説明」の1組。**折り返した2行目は説明の頭に揃えます** ──
    左端まで戻ると、名前と説明が混ざって読めません。

    名前が `name_width` より長ければはみ出させます。切ってしまうと
    CSVの見出しと見比べられなくなるので。
    """
    pad = max(0, name_width - _width_of(name))
    head = f"{' ' * left}{name}{' ' * pad}  "
    body = _wrap(text, _WIDTH - _width_of(head))
    indent = " " * _width_of(head)
    return [head + body[0]] + [indent + line for line in body[1:]]


def _entry(name: str) -> list[str]:
    """列1つぶん。"""
    return _pair(name, DESCRIPTIONS.get(name, ""))


def _rule(char: str = "-") -> str:
    return char * _WIDTH


def _section(filename: str, note: str, fieldnames) -> list[str]:
    out = [_rule(), f" {filename}", f"   … {note}", _rule(), ""]
    for name in fieldnames:
        out.extend(_entry(name))
    out.append("")
    return out


#: 説明の頭に書く「どのCSVの説明か」
WHERE_SAME_FOLDER = " 同じフォルダのCSVの、どの列が何かを書いてあります。"
WHERE_DAY_FOLDERS = " この下の日のフォルダのCSVの、どの列が何かを書いてあります。"


def build(title: str, sections: list[tuple[str, str, list[str]]],
          where: str = WHERE_SAME_FOLDER) -> str:
    """説明の本文。`sections` は (ファイル名, 一言, 列の並び)。"""
    out = [_rule("="), f" {title}", _rule("="), "",
           " この1枚は説明です。数字は入っていません。", where, ""]

    for filename, note, fieldnames in sections:
        out.extend(_section(filename, note, fieldnames))

    out += [_rule("="), " 間違えやすいところ(上の ★ が付いた列)", _rule("="), ""]
    for heading, pairs, closing in CONFUSABLE:
        out.append(f" ● {heading}")
        out.append("")
        for name, text in pairs:
            out.extend(_pair(name, text, left=5, name_width=22))
        out.append("")
        out.extend(f"     {line}" for line in _wrap(closing, _WIDTH - 5))
        out.append("")

    out += [_rule("="), " どのCSVにも共通すること", _rule("="), ""]
    for note in COMMON_NOTES:
        wrapped = _wrap(note, _WIDTH - 5)
        out.append(f" ・ {wrapped[0]}")
        out.extend(f"    {line}" for line in wrapped[1:])
    out.append("")
    # **行末の空白を残さない。** メモ帳では見えませんが、差分や検索の
    # ときに引っかかります
    return "\r\n".join(line.rstrip() for line in out)


def daily_sections() -> list[tuple[str, str, list[str]]]:
    """日ごとの3本と、年月のフォルダの計算内容。列の並びはCSVを書いている
    定義そのものから。

    **ラインの名前を入れません。** 列の説明はラインで変わらないので、
    `<ライン>` のままにします。
    """
    from ..logic import formula

    from . import csv_export

    return [
        ("集計_<日付>_<ライン>.csv",
         "直ごとの合計(1直/2直/3直/日勤で1行ずつ)",
         csv_export.FIELDNAMES),
        ("停止内訳_<日付>_<ライン>.csv",
         "止まった時間を直ごと・記号ごとに。下に1件ずつ(ページ・行)が付きます",
         csv_export.STOP_FIELDNAMES),
        ("集計明細_<日付>_<ライン>.csv",
         "打った行ぜんぶ。紙に載らない用途コード・納入先などもここ",
         csv_export.DETAIL_FIELDNAMES),
        (f"{GUIDE_FILENAME}(この年月のフォルダに1つ)",
         "式の形と注記。数は入っていません(その日の数は 集計_ の列)",
         list(formula.GUIDE_FIELDNAMES)),
    ]


def monthly_sections(year: int, month: int, line: str
                     ) -> list[tuple[str, str, list[str]]]:
    """月別フォルダの3本。日ごとと同じ列ですが、名前が違います。"""
    from . import csv_export, month_export

    stem = f"{year:04d}年{month:02d}月_{line}"
    return [
        (f"{stem}_集計.csv", "その月の直ごとの合計", csv_export.FIELDNAMES),
        (f"{stem}_停止集計.csv", "その月の停止を、直ごと・記号ごとに",
         list(month_export.STOP_HEADERS)),
        (f"{stem}_明細.csv", "その月に打った行ぜんぶ",
         csv_export.DETAIL_FIELDNAMES),
    ]


def month_text(year: int | None = None, month: int | None = None) -> str:
    """年月のフォルダに置く本文。**日付は付けません**(どの日にも当てはまる)。"""
    when = f"  ({year}年{month}月)" if year and month else ""
    return build(f"集計CSVの読み方{when}", daily_sections(), WHERE_DAY_FOLDERS)


def monthly_text(year: int, month: int, line: str) -> str:
    """月別フォルダに置く本文。"""
    return build(f"集計CSVの読み方  ({year}年{month}月 / {line})",
                 monthly_sections(year, month, line))


def _write(folder: Path, text: str) -> Path:
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / FILENAME
    # newline="" で改行をそのまま(本文が CRLF を持っています)
    with open(path, "w", encoding="utf-8-sig", newline="") as f:
        f.write(text)
    return path


def guide_text() -> str:
    """計算内容.csv の中身(CRLF。BOM は書くときに付ける)。"""
    import csv
    import io

    from ..logic import formula

    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=formula.GUIDE_FIELDNAMES,
                            lineterminator="\r\n")
    writer.writeheader()
    for row in formula.guide_rows():
        writer.writerow(row)
    return buf.getvalue()


# ======================================================================
# 消えていないか見る ── **消されたら、次に触ったときに出し直す**
#
# 共有のフォルダに置くものなので、消されます。「よく分からないファイル」
# として片付けられることも、フォルダごと整理されることもあります。
#
# **中身が違うときも出し直します。** 版が上がって列が増えたのに、説明
# だけ前のままというのが、いちばん困る形なので ── 読んだ人は、書いて
# あるとおりだと思って数字を扱います。
#
# 中身が合っていれば書きません。共有のフォルダへの書き込みは高いので、
# 「毎回上書き」にはしません(更新日時が毎日動くのも、見ていて気持ちが
# 悪いものです)。
# ======================================================================
def current_text(folder: Path) -> Optional[str]:
    """いま置いてある本文。無ければ None、読めなければ空文字。

    **`newline=""` で読みます。** 既定のままだと Python が CRLF を LF に
    直して返すので、書いた本文(CRLF)と必ず食い違い、**毎回書き直す**
    ことになります。
    """
    path = folder / FILENAME
    try:
        if not path.is_file():
            return None
        with open(path, encoding="utf-8-sig", newline="") as f:
            return f.read()
    except OSError:                               # 共有に届かない・壊れている
        return ""


def ensure(folder: Path, text: str) -> bool:
    """無い / 中身が違うときだけ書く。**書いたら True。**"""
    if current_text(folder) == text:
        return False
    _write(folder, text)
    return True


def _ensure_file(path: Path, text: str) -> bool:
    """`path` が無い / 中身が違うときだけ書く(BOM付きUTF-8)。書いたら True。"""
    try:
        if path.is_file():
            with open(path, encoding="utf-8-sig", newline="") as f:
                if f.read() == text:
                    return False
    except OSError:
        pass
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8-sig", newline="") as f:
        f.write(text)
    return True


def ensure_month(folder: Path, year: int | None = None,
                 month: int | None = None) -> list[Path]:
    """年月のフォルダに `CSVの読み方.txt` と `計算内容.csv`。**無いときだけ書く。**

    中身が違うとき(版が上がって列が増えた)も書き直します ── 説明だけ古い
    のがいちばん困るので。合っていれば触りません(更新日時も動かない)。
    書いたファイルを返します(空なら何も書いていない)。
    """
    written: list[Path] = []
    if ensure(folder, month_text(year, month)):
        written.append(folder / FILENAME)
    if _ensure_file(folder / GUIDE_FILENAME, guide_text()):
        written.append(folder / GUIDE_FILENAME)
    return written


def ensure_monthly(folder: Path, year: int, month: int, line: str) -> bool:
    return ensure(folder, monthly_text(year, month, line))


# ----------------------------------------------------------------------
# 過ぎた月のぶんも見に行く
#
# 書くたびに確かめるのは**その日の年月のフォルダだけ**です。先月のぶんが
# 消されても、そこへはもう書かないので気づけません ── ボタンを押した
# ときに、出力先の下を一通り見て回ります(CSVの入った日のフォルダがある
# 年月のフォルダだけ)。
# ----------------------------------------------------------------------
@dataclass(frozen=True)
class Sweep:
    """見て回った結果。**何を直したかを画面とログに出す。**"""

    checked: int = 0
    reissued: tuple[Path, ...] = ()
    failed: tuple[tuple[Path, str], ...] = ()

    @property
    def message(self) -> str:
        if not self.checked:
            return ""
        names = f"{FILENAME}・{GUIDE_FILENAME}"
        if self.failed:
            return (f"{names} を {len(self.reissued)}本 置き直しました"
                    f"(書けなかった年月のフォルダが {len(self.failed)}か所)")
        if self.reissued:
            return (f"年月のフォルダの {names} が欠けていたので "
                    f"{len(self.reissued)}本 置き直しました")
        return ""

    def as_dict(self) -> dict:
        return {"checked": self.checked,
                "reissued": [str(p) for p in self.reissued],
                "failed": [[str(p), why] for p, why in self.failed],
                "message": self.message}


#: 日のフォルダの深さ。`<出力先>/ライン/種類/年月/日`
#
# ラインと種類で分けるようになったぶん、2段深くなりました
# (`csv_export.dated_dir`)。ここを直し忘れると、見て回っても
# **1か所も見つからない**まま「異常なし」と出ます。
_DATE_DEPTH = 4


def _day_parts(folder: Path) -> tuple[int, int, int] | None:
    """`…/2026.09/12` から (年, 月, 日) を読む。形が違えば ``None``。"""
    month_part, day_part = folder.parts[-2], folder.parts[-1]
    year_text, _, month_text = month_part.partition(".")
    if not (year_text.isdigit() and month_text.isdigit() and day_part.isdigit()):
        return None
    if len(year_text) != 4:
        return None
    return int(year_text), int(month_text), int(day_part)


def day_folders(base_dir: Path) -> list[Path]:
    """`<出力先>/ライン/種類/年月/日` の形で、CSVが入っているフォルダ。

    **CSVの無いフォルダには置きません。** 説明だけが残っているのは、
    「ここに何かあったはず」と思わせるだけです。

    ラインと種類の段は**名前を見ません**。ラインの名前はマスタで
    増えますし、種類も後から足りうるので、ここで一覧を持つと
    「増やしたのにここだけ古い」が起きます ── 深さと、末尾2段が
    日付の形をしていることだけで判断します。
    """
    found: list[Path] = []
    try:
        for path in sorted(base_dir.glob("/".join(["*"] * _DATE_DEPTH))):
            if not path.is_dir():
                continue
            if _day_parts(path) is None:
                continue
            if any(path.glob("*.csv")):
                found.append(path)
    except OSError:                               # 共有に届かない
        return []
    return found


def month_folders(base_dir: Path) -> list[tuple[Path, int, int]]:
    """CSVの入った日のフォルダがある**年月のフォルダ**と、その年・月。"""
    seen: dict[Path, tuple[int, int]] = {}
    for day in day_folders(base_dir):
        parts = _day_parts(day)
        if parts is None:                         # day_folders が通した後なので来ない
            continue
        seen.setdefault(day.parent, (parts[0], parts[1]))
    return [(folder, y, m) for folder, (y, m) in seen.items()]


def sweep(base_dir: Path) -> Sweep:
    """出力先の下を見て回り、年月のフォルダの説明が欠けている(古い)ところへ置き直す。"""
    reissued: list[Path] = []
    failed: list[tuple[Path, str]] = []
    folders = month_folders(base_dir)
    for folder, year, month in folders:
        try:
            reissued.extend(ensure_month(folder, year, month))
        except OSError as exc:                    # 1か所で止めない
            failed.append((folder, str(exc)))
    return Sweep(checked=len(folders), reissued=tuple(reissued),
                 failed=tuple(failed))
