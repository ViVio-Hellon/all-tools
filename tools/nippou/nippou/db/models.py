"""Dataclasses mirroring the ``daily_header`` / ``daily_detail`` rows.

Field names follow the original Access column names (``NippouDB_EnsureTables``)
translated to snake_case; the detail field order matches
``NippouDB_BuildDetailSQL`` exactly so the two stay easy to cross-reference.
"""
from __future__ import annotations

from dataclasses import dataclass
from dataclasses import field as dc_field

DETAIL_FAMILIES = (
    "lot", "zai", "siz", "ken", "kz", "kh", "sz", "sh", "hit", "ai",
    "mai", "tut", "vc", "et", "s", "th", "ss", "ths", "sth", "tht",
    "s4", "th4", "s5", "th5",
    "con", "wei", "tim", "uni",
)


@dataclass
class HeaderRecord:
    report_date: str
    line: str
    shift: str
    page: int = 1
    worker: str = ""
    day_shift: str = ""  # "有"/"無"
    count: str = ""
    weight_kg: str = ""
    lot_count: str = ""
    coefficient_lot_count: str = ""
    reason: str = ""
    saved_at: str = ""
    dirty: bool = True
    synced_at: str | None = None

    def key(self) -> tuple[str, str, str, int]:
        return (self.report_date, self.line, self.shift, self.page)


# ======================================================================
# 集計フォーマット (VBA の「集計シート」を値にしたもの)
#
# 打つのは `HeaderRecord` / `DetailRecord` のほうだけです。ここから下は
# **保存のたびに作り直される投影**で、`services/summary.py` が作ります。
# ======================================================================

#: `packing_report` の値の列。**表の列順もこの順**
REPORT_FIELDS: tuple[str, ...] = (
    "pages", "worker_name", "daytime_operation", "reason",
    "operation_time", "work_time", "operating_time", "operating_rate",
    "equipment_stop_total", "setup_stop_total", "handling_stop_total",
    "total_lot_count", "coefficient_lot_count",
    "total_quantity", "total_weight", "productivity",
)


@dataclass
class PackingReport:
    """1作業日・1ライン・1直ぶんの集計 (VBA ``Aggre_Calcul`` の結果)。

    **数は数のまま持ちます。** 日報のほう(`HeaderRecord`)は画面から来た
    文字をそのまま置きますが、こちらは計算した結果なので、丸めるのは
    画面や紙に出す直前だけにします ── 早く丸めると、足し合わせたときに
    端数が積もって「計算が合わない」になります。
    """

    work_date: str
    line_name: str
    shift: str
    id: int = 0
    pages: int = 0
    worker_name: str = ""
    daytime_operation: str = ""
    reason: str = ""
    operation_time: float = 0.0          # 操業時間 = 1440 - 管理ロス
    work_time: float = 0.0
    operating_time: float = 0.0          # 稼働時間 = 1440 - 停止ぜんぶ
    operating_rate: float = 0.0
    equipment_stop_total: float = 0.0    # 管理ロス設備停止
    setup_stop_total: float = 0.0        # 段取り・突発停止
    handling_stop_total: float = 0.0
    total_lot_count: int = 0
    coefficient_lot_count: float = 0.0
    total_quantity: float = 0.0
    total_weight: float = 0.0            # Kg
    productivity: float = 0.0            # t/h
    dirty: bool = True
    synced_at: str | None = None
    created_at: str = ""
    updated_at: str = ""

    def key(self) -> tuple[str, str, str]:
        return (self.work_date, self.line_name, self.shift)

    @property
    def total_weight_ton(self) -> float:
        return self.total_weight / 1000.0

    @property
    def stop_total(self) -> float:
        return (self.equipment_stop_total + self.setup_stop_total
                + self.handling_stop_total)


@dataclass
class PackingDetail:
    """1ロット1行 (VBA ``Agg_OutPut`` が集計シートへ並べていたもの)。

    紙に載らない用途コード・用途名・納入先・包装仕様NO・コイル縦割/
    横縦割も入ります ── **集計だけが残す場所**だったので。
    """

    report_id: int = 0
    id: int = 0
    page: int = 1
    row_no: int = 0
    lot_no: str = ""
    material_condition: str = ""
    dimension: str = ""
    incoming_quantity: float = 0.0
    start_hour: int | None = None
    start_minute: int | None = None
    end_hour: int | None = None
    end_minute: int | None = None
    worker_count: float = 0.0
    interleaf: str = ""
    packing_quantity: float = 0.0
    packing_package_count: float = 0.0
    vc_type: str = ""
    actual_quantity: float = 0.0
    actual_weight: float = 0.0
    work_time: float = 0.0
    unit_weight: float = 0.0
    coefficient_lot_count: float = 0.0
    purpose_code: str = ""
    purpose_name: str = ""
    delivery_destination: str = ""
    packing_spec_no: str = ""
    etc: str = ""
    coil_vertical_split: str = ""
    coil_horizontal_split: str = ""
    hiki_no: str = ""
    created_at: str = ""
    updated_at: str = ""
    stops: list["PackingStop"] = dc_field(default_factory=list)


@dataclass
class PackingStop:
    """1停止1行。シートの作業停止①②③を**縦に**したもの。"""

    stop_no: int
    detail_id: int = 0
    id: int = 0
    stop_code: str = ""
    stop_reason: str = ""
    stop_kind: str = ""
    stop_minutes: float = 0.0
    created_at: str = ""
    updated_at: str = ""


@dataclass
class DetailRecord:
    report_date: str
    line: str
    shift: str
    page: int
    row_no: int
    lot: str = ""
    zai: str = ""
    siz: str = ""
    ken: str = ""
    kz: str = ""
    kh: str = ""
    sz: str = ""
    sh: str = ""
    hit: str = ""
    ai: str = ""
    mai: str = ""
    tut: str = ""
    vc: str = ""
    et: str = ""
    s: str = ""
    th: str = ""
    ss: str = ""
    ths: str = ""
    sth: str = ""
    tht: str = ""
    # 作業停止④⑤(v4.24.0)。**紙(xlsx)と VBA には欄が無い**(③まで)。
    # 打つ画面と、手元・共有の日報と、集計に入ります
    s4: str = ""
    th4: str = ""
    s5: str = ""
    th5: str = ""
    con: str = ""
    wei: str = ""
    tim: str = ""
    uni: str = ""
    others1: str = ""
    others2: str = ""
    others3: str = ""
    others4: str = ""
    others5: str = ""
    others6: str = ""
    keisu: str = ""
    # その行の理由(紙の「ヨ：その他（理由を記載）」)。
    #
    # **紙は欄が1つしかなかっただけ**で、書きたいのは行ごとの話です。
    # 停止理由で「その他」を選ぶたび、その行の理由として残します。
    # 紙と共有へ出すときは行番号を付けて1つにまとめます
    # (`logic/reasons.combine`)
    reason: str = ""
    # 選んだ引当番号。**この端末の中だけ**(共有の日報管理へは送らない)。
    # 紙にも Access にも欄が無く、「どの引当で埋めたか」を後から見るための控え
    hiki_no: str = ""
    # Gコースのロットだった印(`logic/g_course` ── `GSS` / `GSS/製造`)。**この端末の中だけ**。
    # 寸法が BOX最終実績から来たことを、開き直しても寸法の欄に出すための控え(v4.17.0)
    box_course: str = ""

    def others(self) -> list[str]:
        return [self.others1, self.others2, self.others3, self.others4, self.others5, self.others6]
