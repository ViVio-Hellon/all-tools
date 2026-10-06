"""ライン毎の目標枚数を読む / 取り込む

【CSVが正】
目標を変えるのに Access なりツールなりを開くのは、現場の手間として
重すぎます ── **メモ帳で直せる CSV** を正にして、共有に1つ置けば
全端末に効くようにします。置き場所と名前は設定画面から変えられます
(`config.SETTINGS.line_target_path`)。

【出どころは2つ。CSVが勝つ】
目標は**マスタ(梱包資材マスタ「ライン毎目標」)とCSVの両方から読みます。**
同じラインが両方にあれば**CSVを採ります** ── 利用者の言葉で
「DBの書き換えよりCSVの書き換えのほうが簡単だから」。マスタは共有の
DBで、直すには道具と権限が要りますが、CSVはメモ帳で開けます。

勝ち負けは**ラインごと**です。ファイルごとにすると、今月 L-1 だけ
上げたいときに CSV へ1行書いた瞬間、**他のラインの目標が全部消えます**
── 直したい1行だけ書けば済む、が「CSVのほうが簡単」の中身なので、
そこを壊しません(`logic/line_target.merge`)。

【混ぜるなら、どちらが勝ったかを見せる】
2つ読むと「マスタを直したのに変わらない」(CSVが勝っている)が起こり得ます。
これは混ぜ方の問題ではなく**見えないことの問題**なので、値ごとに出どころ
(`Targets.origins`)を持って、設定画面の表にそのまま出します。

【ライン名について】
マスタは `L-1` `機側` `ﾄｯﾄ` `ﾊﾞﾗﾝｻｰ`。v4.13.0 からツールの名前も正規の呼び名
(`L-1` `機側` `トット` `バランサー`)なので、揃えるのは文字の幅だけです
(**`logic/line_names.DEFINITIONS`** の表。アクセス権限と同じ表)。表に無い呼び名
(`予備PC` など)はそのまま残します。CSV に v4.12 までの名前(`LS` など)で書いて
あっても、CSV の読み方(`logic/line_target.normalize_line`)が正規へ読み替えます。
"""
from __future__ import annotations

from pathlib import Path
from typing import Optional

from ..config import SETTINGS
from ..logging_setup import get_logger
from ..logic import line_target as lt

log = get_logger("services.targets")

#: 目標CSVの文字。**BOM付きUTF-8。**
#:
#: 付けないと Windows のメモ帳も Excel も cp932 だと思って開くので、
#: 日本語が化けます(`繝ｩ繧､繝ｳ豈弱・逶ｮ讓・`)。読むほうは utf-8-sig →
#: utf-8 → cp932 の順に試すので(`_read_text`)、現場が cp932 で
#: 保存し直しても読めます ── **書くときだけ**こちらで揃えます。
CSV_ENCODING = "utf-8-sig"

#: 取り込んだCSVの頭に書く覚え書き
CSV_NOTE = ("ライン毎の目標枚数(1日あたりの枚数)。グラフの45度線に使います。\n"
            "このファイルをメモ帳で直せば、次にグラフを開いたときから効きます。\n"
            "書き方: ライン名,目標   行頭 # は覚え書き。\n"
            "ライン名は日報ツールの呼び名で書いてください。")


def load(path: Optional[Path] = None) -> lt.Targets:
    """**マスタとCSVの両方**を読んで、ラインごとに重ねる(CSVが勝つ)。

    どちらか片方しか読めなくても、読めたほうで返します ── 目標が
    読めないことを理由に画面を止めません(グラフは実績だけ出ます)。

    どの値がどちらから来たかは `Targets.origins` に入るので、設定画面で
    確かめられます。
    """
    from_csv = load_csv(path)
    from_master = read_master()
    return lt.merge(from_master, from_csv,
                    base_name=lt.FROM_MASTER, top_name=lt.FROM_CSV)


def load_csv(path: Optional[Path] = None) -> lt.Targets:
    """CSVだけを読む。**読めなくても空で返します**(グラフは出す)。

    目標が無ければ線が引かれないだけで、実績のグラフは今までどおり
    出ます ── 目標が読めないことを理由に画面を止めません。
    どこを見たか・何が読めなかったかは戻り値に入るので、設定画面で
    確かめられます。
    """
    target_path = Path(path) if path else SETTINGS.line_target_path
    try:
        if not target_path.exists():
            found = lt.Targets(source=str(target_path))
            # **無くても困りません。** マスタのほうが読めていれば目標は
            # 出ます ── CSVは「マスタと違う値にしたいとき」に置くもの
            found.problems.append(lt.Problem(
                0, str(target_path),
                "まだありません(マスタの値がそのまま効きます)"))
            return found
        text = _read_text(target_path)
    except OSError as exc:                        # 共有に届かない等
        log.warning("目標CSVを読めませんでした path=%s error=%s",
                    target_path, exc)
        found = lt.Targets(source=str(target_path))
        found.problems.append(lt.Problem(0, str(target_path), f"読めません: {exc}"))
        return found
    return lt.parse(text, source=str(target_path))


def _read_text(path: Path) -> str:
    """文字の読み方。**Windows のメモ帳で保存されたものを読めるように。**

    UTF-8(BOM付きも)を先に試し、だめなら cp932。現場のメモ帳は
    既定が cp932 のことがあり、そこで落とすと「直したのに読まれない」に
    なります。
    """
    raw = path.read_bytes()
    for encoding in ("utf-8-sig", "utf-8", "cp932"):
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace")


def of(line: str, path: Optional[Path] = None) -> Optional[float]:
    """そのラインの目標(1日あたりの枚数)。無ければ None。"""
    return load(path).of(line)


# ------------------------------------------------------------------
# マスタからの取り込み
# ------------------------------------------------------------------
def read_master() -> lt.Targets:
    """梱包資材マスタ「ライン毎目標」を読む(取り込み用)。

    VBA `目標値抜き取り` が見ていた表です。読めなければ空で返します。
    """
    from ..access_bridge.importer import import_table

    source = f"{SETTINGS.gw_material_master_path} の「{SETTINGS.line_target_table}」"
    try:
        result = import_table(SETTINGS.gw_material_master_path,
                              SETTINGS.line_target_table)
        if not result.success:
            found = lt.Targets(source=source)
            found.problems.append(lt.Problem(0, source,
                                             f"読めません: {result.error}"))
            return found
        rows = result.rows
    except Exception as exc:                      # noqa: BLE001 - 画面は出す
        log.exception("ライン毎目標マスタを読めませんでした")
        found = lt.Targets(source=source)
        found.problems.append(lt.Problem(0, source, f"読めません: {exc}"))
        return found

    # 正規の呼び名(機側・ﾄｯﾄ など)をツールの名前(正規の字)へ。**定義の表だけで**読む
    # (`logic/line_names`)。ラインのつもりで読めない呼び名(L1・l-1 など)は**読まずに
    # 困りごととして残し**(v4.12.3 ── そのまま通すと、CSV の読み方で L-1 として効いて
    # しまう)、ラインでない呼び名(予備PC など)はこれまでどおり残す
    from ..logic import line_names

    renamed, skipped = [], []
    for number, row in enumerate(rows, 1):
        name = row.get("ライン")
        code = line_names.to_code(name)
        if code:
            renamed.append({**row, "ライン": code})
            continue
        mistake = line_names.check(name)
        if mistake:
            skipped.append(lt.Problem(number, str(name or ""), mistake))
            continue
        renamed.append(row)
    found = lt.from_rows(renamed, source=source)
    found.problems += skipped
    return found


def import_from_master(path: Optional[Path] = None,
                       order: tuple[str, ...] = ()) -> lt.Targets:
    """マスタを読んでCSVへ書き出す。**上書きします。**

    初期値づくり用です。以降はCSVのほうを直してもらいます ──
    取り込みボタンを押さない限り、マスタの値がCSVを上書きすることは
    ありません(黙って戻る、が起きないように)。
    """
    from .. import constants

    target_path = Path(path) if path else SETTINGS.line_target_path
    found = read_master()
    if not found.values:
        return found
    target_path.parent.mkdir(parents=True, exist_ok=True)
    # **BOM付きで書きます。** 付けないと Windows のメモ帳も Excel も
    # cp932 だと思って開き、`ライン毎目標` が `繝ｩ繧､繝ｳ豈弱・逶ｮ讓・` に
    # なります ── 「メモ帳で直せる」が売りのファイルなので、開いた瞬間に
    # 読めないのでは意味がありません(`CSV_ENCODING`)
    target_path.write_text(
        lt.as_csv(found, order=order or constants.LINE_NAMES, note=CSV_NOTE),
        encoding=CSV_ENCODING)
    log.info("ライン毎目標を取り込みました path=%s 件数=%d",
             target_path, len(found.values))
    found.source = str(target_path)
    return found
