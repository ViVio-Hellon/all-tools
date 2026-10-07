"""GW計算フォーム(梱包資材重量計算)用のマスタ参照。

VBAの ``寸法表示``（LotNo検索、標準モジュール~7868行）/ ``VC選択``
（~8868行）/ ``合紙選択``（~8959行）は、Excelブックにキャッシュした
"LOT一覧"/"VC一覧" シートと Access "SIKAHIKI.accdb"（引当）を
突き合わせていた。ここではそのキャッシュ役を SQLite ではなく
毎回 Access への直接問い合わせ（``access_bridge.importer``）に置き換え、
"LOT一覧" = ``SIKALOT.accdb``、"VC一覧" = ``SIKAODR.accdb`` の
「仕掛」テーブルを直接検索する。

いずれも参照専用（読み取りのみ）で、③反映(Push)の対象には含まれない。

資材重量マスタ・VC重量マスタ（``梱包資材マスタ.accdb``）は提出データで
実カラム構成を確認済み: 資材重量=[管理番号/梱包資材名/単位質量/係数/単位/
単位量/備考]、VC重量=[管理番号/品名/単位質量/係数/単位/単位量/更新日]。
``GetUnitMassAndCoefficient`` と同じ「単位質量×係数」の考え方をVC重量側
にも適用している（詳細は :func:`lookup_vc_rate`）。同ファイルには
班員名簿テーブルも同居しており、人員フォームの ``access_bridge.staff_master``
から参照する。
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from ..config import SETTINGS
from ..logging_setup import get_logger
from ..logic import g_course
from ..logic.gw_calculation import ZERO_RATE, MaterialRate
from . import script_gen
from .importer import import_table
from .runner import ScriptRunner

_logger = get_logger("access_bridge.gw_master")


def _to_float(value: Optional[str]) -> float:
    try:
        return float((value or "").strip())
    except ValueError:
        return 0.0


@dataclass
class LotInfo:
    """port of ``寸法表示`` が LOT一覧(=SIKALOT の「仕掛」)から拾う項目。

    日報入力の ``SQLiteLot検索`` も同じ表を読みますが、拾う列が少し多い
    (用途名・BOX最終実績_枚本数と寸法・梱包_合紙・鋳造番号)。**同じ表を2か所から
    別々に読まない**ので、ここに足しています ── 足りない列は空で返る
    だけなので、GW計算側の呼び出しは変わりません。
    """

    lot_no: str
    order_no: str
    inspection_no: str
    material: str  # 製造材質
    temper: str  # 製造調質
    thickness_mm: float  # 製造板厚
    width_mm: float  # 製造板幅
    length_mm: float  # 製造板丈
    usage_code: str  # 用途ｺｰﾄﾞ
    course: str  # 設計_設備ｺｰｽ
    customer: str  # 取引先名
    delivery: str  # 納入先名
    sender: str  # 送り先名
    # --- 日報入力(`SQLiteLot検索`)が使う分 -------------------------
    usage_name: str = ""       # 用途名
    #: 検入枚数(KEN)。**BOX最終実績_枚本数**(そのロットの最終工程の実績 ── v4.16.0)。
    #: 空・0 なら BOX実績_枚本数(`_box_final`)
    box_sheets: str = ""
    interleaf_flag: str = ""   # 梱包_合紙("1" なら有)
    cast_no: str = ""          # 鋳造番号
    #: BOX最終実績_板厚・板幅・板丈(v4.16.0)。BOXコースの寸法(`size_for_report`)
    box_thickness_mm: float = 0.0
    box_width_mm: float = 0.0
    box_length_mm: float = 0.0

    @property
    def box_course(self) -> str:
        """設計_設備ｺｰｽ に含まれる Gコース(GCT / GFS / GSS)。無ければ空。"""
        return g_course.find_course(self.course)

    @property
    def is_box(self) -> bool:
        return bool(self.box_course)

    def product_size(self) -> tuple[float, float, float]:
        """製品の寸法(厚・幅・丈)。**Gコースなら BOX最終実績の寸法**(v4.16.0)。

            設計_設備ｺｰｽに GCT / GFS / GSS のどれかが含まれていた場合
            BOX最終実績_板厚、BOX最終実績_板幅、BOX最終実績_板丈 が寸法になるように
            GWも同様にしてください(v4.17.0)

        日報の製品寸法・GW計算・包装仕様の注意が同じ寸法を見ます。BOX最終実績が
        3つとも 0 なら製造の寸法のまま(`logic/g_course`)。
        """
        return g_course.product_size(self)


@dataclass
class Allocation:
    """引当1件(SIKAHIKI の「仕掛」)。

    **ロット番号から受注番号を知る道はここしかありません。** VBA
    `SQLiteLot検索` は同じロット番号で最初に見つかった1件だけを黙って
    使っていましたが、実際には1つのロットに複数の引当が付きます
    (別の受注へ分けて出す)。どれを使うかで VC も単重も合紙も変わるので、
    複数あるときは選ばせます(`logic/lot_fill` の説明)。
    """

    lot_no: str
    order_no: str        # 受注番号
    hiki_no: str = ""    # 引当番号(8桁。数値にすると指数表記になるので文字列)
    quantity: str = ""   # 引当数量
    adjust_no: str = ""  # 引当調整NO

    @property
    def label(self) -> str:
        """選ぶときに出す1行。**番号だけでは選べない**ので数量も並べる。"""
        amount = self.adjust_no or self.quantity or "全量"
        return f"引当 {self.hiki_no} / 受注 {self.order_no} / {amount}"


@dataclass
class OrderInfo:
    """port of ``VC選択``/``合紙選択``/``寸法表示`` が VC一覧
    (=SIKAODR の「仕掛」)から拾う項目。"""

    order_no: str
    vc_front: str  # VC_表
    vc_back: str  # VC_裏
    has_interleaf: bool  # 合紙 (VBAでは "1" のとき True)
    unit_weight_kg: float  # 製品単重
    customer: str  # 取引先名称
    delivery: str  # 納入先名称
    sender: str  # 送り先名称
    # 包装仕様NO。``バンド選択`` が "7P0106" を PETバンド+縦1本に、
    # ``CommandButton2_Click`` が同じ番号をEX2方向チェックの例外にする。
    # **どちらもこの1列で決まる**ので、取れないと自動選択が効かなくなる
    pack_spec_no: str = ""
    # 合紙の生値("1" なら使う)。判定は `logic/gw_autoselect.select_interleaf`
    interleaf_flag: str = ""
    # EX_輸出区分。**輸出かどうかがここで決まる**ので、日報の etc 欄の
    # 「EX」を自動で立てるのに使う(`logic/etc_marks`)
    export_flag: str = ""


#: BOX最終実績の寸法に切り替える設計_設備ｺｰｽ(部分一致)。**決めているのは
#: `logic/g_course.COURSES`**(外注の印・python-web-tools と同じ3つ)
BOX_COURSES: tuple[str, ...] = g_course.COURSES


def _box_final(row: dict, name: str) -> str:
    """BOX最終実績_{name}。空・0(列が無い古い写し・最終工程がまだ無い)なら BOX実績_{name}。

    1件検索が読むのはロットの先頭行で、BOX実績_* はたいてい1工程目(鋳塊1本の「1」)。
    **最終実績**がそのロットの本当の枚数・寸法です(python-web-tools と同じ読み方)。
    """
    final = str(row.get(f"BOX最終実績_{name}", "") or "").strip()
    if final and _to_float(final) != 0:
        return final
    return str(row.get(f"BOX実績_{name}", "") or "").strip()


def _search_wip(paths: list[Path], sql_filter: str, runner: Optional[ScriptRunner],
                label: str, keep=None) -> list[dict]:
    """仕掛のファイルを**1つ目 → 2つ目**の順に引く(v4.23.0)。最初に見つかった行を返す。

    次へ進むのは:
      - ファイルが無い・読めない
      - ファイルはあるが、探すもの(ロット・引当・受注)が無い
    2つ目を決めていなければ1つ目だけ(これまでどおり)。`keep` は使える行だけに絞る
    (引当なら受注番号のある行)。絞って空なら「無い」とみなして次へ。
    """
    failure: Optional[Exception] = None
    answered = False
    for index, path in enumerate(paths):
        try:
            result = import_table(path, SETTINGS.gw_shared_table_name,
                                  sql_filter=sql_filter, runner=runner)
        except Exception as exc:                  # noqa: BLE001 - 次の置き場所を見る
            _logger.warning("%sで読めませんでした(%dつ目 %s): %s", label, index + 1, path, exc)
            failure = exc
            continue
        answered = True
        if not result.success:
            _logger.warning("%sに失敗しました(%dつ目 %s) error=%s",
                            label, index + 1, path, result.error)
            continue
        rows = keep(result.rows) if keep else list(result.rows)
        if rows:
            if index > 0:
                _logger.info("%s: 1つ目に無かったので %dつ目で見つけました(%s)",
                             label, index + 1, path)
            return rows
    if failure is not None and not answered:
        # どの置き場所でも読めなかった。**「無い」とは言わない**(呼ぶ側が「読めません」と出す)
        raise failure
    return []


def _paths(master_path: Optional[Path], default: list[Path]) -> list[Path]:
    """渡されたファイルがあればそれだけ、無ければ設定の順(1つ目 → 2つ目)。"""
    return [master_path] if master_path else list(default)


def search_lot(
    lot_no: str,
    master_path: Optional[Path] = None,
    runner: Optional[ScriptRunner] = None,
) -> Optional[LotInfo]:
    """port of ``寸法表示()`` のロット検索部分。該当なしなら ``None``
    （VBAの ``fL = False`` → "Text.データなし　手入力よろしく" 警告に相当、
    警告表示自体は呼び出し元のUI層が行う）。"""
    sql_filter = f"[ﾛｯﾄ番号]={script_gen.sql_literal(lot_no, 'TEXT')}"
    rows = _search_wip(_paths(master_path, SETTINGS.gw_lot_master_paths), sql_filter,
                       runner, f"LotNo検索 lot_no={lot_no}")
    if not rows:
        return None

    row = rows[0]
    return LotInfo(
        lot_no=row.get("ﾛｯﾄ番号", ""),
        order_no=row.get("ｵｰﾀﾞｰ番号", ""),
        inspection_no=row.get("検査番号", ""),
        material=row.get("製造材質", ""),
        temper=row.get("製造調質", ""),
        thickness_mm=_to_float(row.get("製造板厚")),
        width_mm=_to_float(row.get("製造板幅")),
        length_mm=_to_float(row.get("製造板丈")),
        usage_code=row.get("用途ｺｰﾄﾞ", ""),
        course=row.get("設計_設備ｺｰｽ", ""),
        customer=row.get("取引先名", ""),
        delivery=row.get("納入先名", ""),
        sender=row.get("送り先名", ""),
        usage_name=row.get("用途名", ""),
        box_sheets=_box_final(row, "枚本数"),
        interleaf_flag=row.get("梱包_合紙", ""),
        cast_no=row.get("鋳造番号", ""),
        box_thickness_mm=_to_float(_box_final(row, "板厚")),
        box_width_mm=_to_float(_box_final(row, "板幅")),
        box_length_mm=_to_float(_box_final(row, "板丈")),
    )


def search_allocations(
    lot_no: str,
    master_path: Optional[Path] = None,
    runner: Optional[ScriptRunner] = None,
) -> list[Allocation]:
    """そのロットの引当を**全部**返す(SIKAHIKI の「仕掛」)。

    VBA `SQLiteLot検索` は `SQLiteFindRow` で**最初の1件**しか見ずに
    受注番号を決めていました。1つのロットが複数の受注に引き当てられて
    いると、そこで拾った受注の VC・単重・合紙が、実際に梱包する相手と
    食い違います ── 押した人には見分けが付きません。

    ここは全部返し、**どれを使うかは呼び出し側(画面)が決めます。**
    1件しか無ければ選ばせる意味も無いので、そのまま使います。

    並びは引当番号の昇順。全件8桁固定なので、文字列のままでも数値順と
    一致します(python-web-tools `lot_service._load_hiki` と同じ理由)。
    """
    sql_filter = f"[ﾛｯﾄ番号]={script_gen.sql_literal(lot_no, 'TEXT')}"
    # 受注番号の無い行は選ばせても意味が無い(そこから先へ進めない)。**そういう行しか
    # 無ければ「引当が無い」とみなして2つ目を見る**
    rows = _search_wip(_paths(master_path, SETTINGS.gw_hiki_master_paths), sql_filter,
                       runner, f"引当の検索 lot_no={lot_no}",
                       keep=lambda rows: [r for r in rows if (r.get("受注番号", "") or "").strip()])

    found = [
        Allocation(
            lot_no=row.get("ﾛｯﾄ番号", ""),
            order_no=(row.get("受注番号", "") or "").strip(),
            hiki_no=(row.get("引当番号", "") or "").strip(),
            quantity=(row.get("引当数量", "") or "").strip(),
            adjust_no=(row.get("引当調整NO", "") or "").strip(),
        )
        for row in rows
    ]
    return sorted(found, key=lambda a: a.hiki_no)


def search_order(
    order_no: str,
    master_path: Optional[Path] = None,
    runner: Optional[ScriptRunner] = None,
) -> Optional[OrderInfo]:
    """port of ``VC選択``/``合紙選択``/``寸法表示`` のオーダー検索部分。"""
    sql_filter = f"[受注番号]={script_gen.sql_literal(order_no, 'TEXT')}"
    rows = _search_wip(_paths(master_path, SETTINGS.gw_order_master_paths), sql_filter,
                       runner, f"オーダーNo検索 order_no={order_no}")
    if not rows:
        return None

    row = rows[0]
    return OrderInfo(
        order_no=row.get("受注番号", ""),
        vc_front=row.get("VC_表", ""),
        vc_back=row.get("VC_裏", ""),
        has_interleaf=row.get("合紙", "").strip() == "1",
        unit_weight_kg=_to_float(row.get("製品単重")),
        customer=row.get("取引先名称", ""),
        delivery=row.get("納入先名称", ""),
        sender=row.get("送り先名称", ""),
        pack_spec_no=row.get("包装仕様NO", ""),
        interleaf_flag=row.get("合紙", ""),
        export_flag=row.get("EX_輸出区分", ""),
    )


@dataclass
class CoilSplit:
    """コイルの割り数 (`LS4LOT` の `当工程設計_縦割数` / `横割数`)。

    紙の印刷範囲のさらに外(AR列・AS列)に出る2つです。VBA も
    `OthersT` の5番目・6番目としてここから入れていました。
    """

    lot_no: str = ""
    vertical: str = ""      # 当工程設計_縦割数 → コイル縦割(others5)
    horizontal: str = ""    # 当工程設計_横割数 → コイル横縦割(others6)


def search_coil_split(
    lot_no: str,
    master_path: Optional[Path] = None,
    runner: Optional[ScriptRunner] = None,
) -> Optional[CoilSplit]:
    """port of ``SQLiteLot検索`` のコイル縦横。該当なしなら ``None``。

    **機側・NS1 のときだけ呼びます**(コイル形状。VBA は AIM も入れていたが
    v4.21.0 で外した ── `logic/lot_fill.COIL_SPLIT_LINES`)。ほかのラインは
    コイルを扱わないので、引く意味がありません。

    VBA はこのファイルだけ `SimpleArr`(=Access のまま)で読んでいました
    が、ほかの参照と同じく **sqlite3 として扱います** ── 上流の
    書き出しが .accdb から .sqlite3 へ移るのに合わせたもので、
    `_pick_source` があるので `.accdb` しか無いフォルダでも読めます。

    **読めなくても例外にしません。** 縦横が空になるだけで、日報の入力
    そのものは続けられます(VBA も `GoTo skipCoilErr` で続けていました)。
    """
    path = master_path or SETTINGS.gw_coil_master_path
    sql_filter = f"[ﾛｯﾄ番号]={script_gen.sql_literal(lot_no, 'TEXT')}"
    try:
        result = import_table(path, SETTINGS.gw_shared_table_name,
                              sql_filter=sql_filter, runner=runner)
    except Exception:                             # noqa: BLE001 - 入力は止めない
        _logger.exception("LS4LOT を読めませんでした lot_no=%s", lot_no)
        return None

    if not result.success or not result.rows:
        if not result.success:
            _logger.warning("コイル縦横の取得に失敗しました lot_no=%s error=%s",
                            lot_no, result.error)
        else:
            # VBA も `DebugLog("LS4LOTヒットなし")` を残して続けていた
            _logger.info("LS4LOT にヒットなし lot_no=%s", lot_no)
        return None

    # VBA は**最後にあたった行**を採っていた(ループで上書き)。
    # 実データでは1ロット1行なので、どちらでも同じ
    row = result.rows[-1]
    return CoilSplit(
        lot_no=row.get("ﾛｯﾄ番号", ""),
        vertical=row.get("当工程設計_縦割数", ""),
        horizontal=row.get("当工程設計_横割数", ""))


def lookup_material_rate(rows: list[dict], material_name: str) -> MaterialRate:
    """port of ``GetUnitMassAndCoefficient``: 資材重量マスタの行一覧から
    ``梱包資材名`` が一致する行の単位質量・係数を返す。一致しなければ
    :data:`ZERO_RATE`（VBAの ``Array("", "")`` に相当、未登録として扱う）。"""
    for row in rows:
        if row.get("梱包資材名") == material_name:
            return MaterialRate(unit_mass=_to_float(row.get("単位質量")), coefficient=_to_float(row.get("係数")))
    return ZERO_RATE


def load_material_rate_rows(
    master_path: Optional[Path] = None,
    runner: Optional[ScriptRunner] = None,
) -> list[dict]:
    """資材重量マスタの全行を取得する。件数は少ない想定のためフィルタなし。"""
    master_path = master_path or SETTINGS.gw_material_master_path
    result = import_table(master_path, SETTINGS.gw_material_master_table, runner=runner)
    if not result.success:
        _logger.warning("資材重量マスタの取得に失敗しました error=%s", result.error)
        return []
    return result.rows


def lookup_vc_rate(rows: list[dict], product_name: str) -> MaterialRate:
    """VC重量マスタから品名一致行の単位質量・係数を返す
    （``資材重量`` マスタと同じ列構成: 品名/単位質量/係数）。"""
    for row in rows:
        if row.get("品名") == product_name:
            return MaterialRate(unit_mass=_to_float(row.get("単位質量")), coefficient=_to_float(row.get("係数")))
    return ZERO_RATE


def load_vc_rate_rows(
    master_path: Optional[Path] = None,
    runner: Optional[ScriptRunner] = None,
) -> list[dict]:
    master_path = master_path or SETTINGS.gw_material_master_path
    result = import_table(master_path, SETTINGS.gw_vc_master_table, runner=runner)
    if not result.success:
        _logger.warning("VC重量マスタの取得に失敗しました error=%s", result.error)
        return []
    return result.rows


def list_vc_product_names(rows: list[dict]) -> list[str]:
    """port of ``VCCombo設定``: ComboBox1/2 に積む品名一覧。"""
    names = [row.get("品名", "") for row in rows]
    return [n for n in names if n]
