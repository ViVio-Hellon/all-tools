"""全停入力・停止理由内訳マスタ（作業停止時間内訳_1/2/3）の参照。

VBAの ``boxin()``（~1905行）/ ``全停入力()``（~5273行）/
``LoadFieldArrayAndColumns``（~1986行）が、Access "伝送用ファイル.accdb"
の3テーブル（設備停止=作業停止時間内訳_1、不稼働=作業停止時間内訳_2、
ハンドリング=作業停止時間内訳_3、いずれも列は「内訳」「内訳番号」）
から読み込んでいた処理を移植したもの。参照専用（読み取りのみ）。

【停止内訳.csv があれば、そちらが先】(v3.93.0)
停止理由を足すのに管理者パスワード(マスタ管理の鍵)が要らないように、
**メモ帳で直せる CSV** を置けばそちらを読みます(`logic/stop_csv`)。
**分類ごとに**、CSVに行があればCSV、1行も無ければ上の表です ── 重ね
ません(重ねると、CSVから消した理由が表から戻ってくる)。

停止理由を読むところ(日報入力の選択欄・全停入力・保存前チェック・
集計の名前)は、どれも `load_all_categories` を通ります。ここを
変えれば全部に効きます。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from ..config import SETTINGS
from ..logging_setup import get_logger
from ..logic import stop_csv
from .importer import import_table
from .runner import ScriptRunner

_logger = get_logger("access_bridge.stop_master")

# port of Table内訳1/2/3 (boxin()) -- カテゴリ名(表示用) -> テーブル名。
CATEGORY_TABLES: dict[str, str] = {
    "設備停止": "作業停止時間内訳_1",
    "不稼働": "作業停止時間内訳_2",
    "ハンドリング": "作業停止時間内訳_3",
}


@dataclass(frozen=True)
class StopReason:
    label: str  # 内訳
    code: str  # 内訳番号


def _read_master(category: str, master_path: Optional[Path],
                 runner: Optional[ScriptRunner]) -> tuple[list[StopReason], str]:
    """表を1つ読む。戻りは (理由, 読めなかった理由)。"""
    table_name = CATEGORY_TABLES.get(category)
    if table_name is None:
        raise ValueError(f"未知のカテゴリです: {category}")

    master_path = master_path or SETTINGS.stop_reason_master_path
    result = import_table(master_path, table_name, runner=runner)
    if not result.success:
        _logger.warning("停止理由マスタの取得に失敗しました category=%s error=%s", category, result.error)
        return [], str(result.error or "読めませんでした")

    reasons: list[StopReason] = []
    for row in result.rows:
        label = row.get("内訳", "").strip()
        if not label:
            continue
        reasons.append(StopReason(label=label, code=row.get("内訳番号", "").strip()))
    return reasons, ""


def load_stop_reasons(
    category: str,
    master_path: Optional[Path] = None,
    runner: Optional[ScriptRunner] = None,
) -> list[StopReason]:
    """port of ``boxin()`` の内訳テーブル読み込み部分（1カテゴリ分）。**表だけ**。"""
    return _read_master(category, master_path, runner)[0]


# ------------------------------------------------------------------
# 停止内訳.csv
# ------------------------------------------------------------------
@dataclass
class Sources:
    """停止内訳の出どころ2つと、実際に使うもの。設定画面が出す。"""

    #: 実際に使う一覧(カテゴリ → 理由)
    effective: dict[str, list[StopReason]] = field(default_factory=dict)
    #: カテゴリ → `stop_csv.FROM_CSV` / `FROM_MASTER` / ""
    origins: dict[str, str] = field(default_factory=dict)
    csv: stop_csv.Parsed = field(default_factory=stop_csv.Parsed)
    csv_path: str = ""
    csv_exists: bool = False
    #: CSVそのものが読めなかった(あるのに開けない)とき
    csv_error: str = ""
    #: 表から読んだもの。**読んでいないカテゴリは入らない**(`read_master`)
    master: dict[str, list[StopReason]] = field(default_factory=dict)
    #: カテゴリ → 表が読めなかった理由
    master_errors: dict[str, str] = field(default_factory=dict)


def read_csv_text(path: Path) -> str:
    """CSVの文字。**メモ帳・Excel で保存されたものを読めるように**
    UTF-8(BOM付きも)→ cp932 の順に試す。"""
    raw = path.read_bytes()
    for encoding in ("utf-8-sig", "utf-8", "cp932"):
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace")


def read_csv(csv_path: Optional[Path] = None) -> tuple[stop_csv.Parsed, bool, str]:
    """CSVを読む。戻りは (読めたもの, あるか, 開けなかった理由)。

    **無くても困りません**(表を読むだけ)。あるのに開けないとき
    (共有に届かない等)も表で動きます ── 停止理由が読めないことを
    理由に日報入力を止めません。
    """
    path = Path(csv_path) if csv_path else SETTINGS.stop_reason_csv_path
    try:
        if not path.is_file():
            return stop_csv.Parsed(source=str(path)), False, ""
        text = read_csv_text(path)
    except OSError as exc:
        _logger.warning("停止内訳CSVを読めませんでした path=%s error=%s", path, exc)
        return stop_csv.Parsed(source=str(path)), False, f"読めません: {exc}"
    return stop_csv.parse(text, source=str(path)), True, ""


def load_sources(
    master_path: Optional[Path] = None,
    runner: Optional[ScriptRunner] = None,
    *,
    csv_path: Optional[Path] = None,
    read_master: bool = False,
) -> Sources:
    """CSVと表を読んで、分類ごとに使うほうを決める(`stop_csv.pick`)。

    表は**CSVに無い分類だけ**読みます(`read_master` で全部読む)── CSVが
    3つとも持っていれば、共有の伝送用ファイルを写しに行きません。
    設定画面は両方を見せるので `read_master=True` で呼びます。
    """
    parsed, exists, error = read_csv(csv_path)
    out = Sources(csv=parsed, csv_path=parsed.source, csv_exists=exists,
                  csv_error=error)
    covered = set(parsed.categories())
    for category in CATEGORY_TABLES:
        if category in covered and not read_master:
            continue
        reasons, why = _read_master(category, master_path, runner)
        out.master[category] = reasons
        if why:
            out.master_errors[category] = why

    master_rows = {c: [stop_csv.Reason(r.code, r.label) for r in rows]
                   for c, rows in out.master.items()}
    picked = stop_csv.pick(parsed, master_rows)
    for category in CATEGORY_TABLES:
        if picked.origins.get(category) == stop_csv.FROM_CSV:
            out.effective[category] = [StopReason(label=r.label, code=r.code)
                                       for r in picked.values[category]]
        else:
            # 表から来たものは**そのまま**(記号の無い行も残す ── 従来どおり)
            out.effective[category] = list(out.master.get(category, []))
        out.origins[category] = picked.origins.get(category, "")
    return out


def load_all_categories(
    master_path: Optional[Path] = None,
    runner: Optional[ScriptRunner] = None,
    *,
    csv_path: Optional[Path] = None,
) -> dict[str, list[StopReason]]:
    """port of ``boxin()`` の3テーブル一括読み込み（ALLSTOP相当の統合元）。

    **停止内訳.csv に行がある分類はCSVから**、無い分類は表から(`load_sources`)。
    """
    return load_sources(master_path, runner, csv_path=csv_path).effective


def find_reason_code(reasons: list[StopReason], label: str) -> str:
    """port of ``全停入力()`` の ``Answer`` 取得部分: 内訳名から内訳番号を検索する。"""
    for reason in reasons:
        if reason.label == label:
            return reason.code
    return ""
