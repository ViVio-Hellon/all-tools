"""梱包資材重量計算 (tkinter版 `ui/gw_window.py` / VBA `UFGW`/`NewGW`)

    GET  /gw                 画面
    POST /api/gw/calculate   計算する(**計算の内訳も返す**)
    POST /api/gw/lot         LotNo で引く
    POST /api/gw/order       オーダーNo で引く

計算式は `logic/gw_calculation.py` がそのまま持つ。ここは入力の形を
整えて渡し、返ってきた数値を画面の形にするだけ。

【計算の内訳を一緒に返す】
VBA の `NewGW` は押すと欄に数字が出るだけで、疑われたときに確かめる手が
**ソースを読む**しかありませんでした。`presenters/gw_breakdown.py` が
「式 → 数字を入れた式 → 基準量 → × 単位質量 × 係数 = 重量」の4段を
組み立てるので、画面でそのまま開けます。
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime
from typing import Any, Optional

from flask import Blueprint, Response, jsonify, render_template, request

from nippou import work_context
from nippou.access_bridge import gw_master
from nippou.logging_setup import get_logger, log_button_click
from nippou.logic import g_course
from nippou.logic import gw_autoselect as autoselect
from nippou.logic import gw_calculation as gw
from nippou.logic import packing_figure
from nippou.reporting import gw_print

from .. import error_body, shell

log = get_logger("app.routes.gw")

bp = Blueprint("gw", __name__)

# 画面の入力欄。名前は `logic.gw_calculation.PackingDimensions` の
# フィールド名と**そろえる** ── 別の名前を挟むと、対応表をここと画面の
# 2か所で持つことになる。
#
# 【縦に並べて、列で読ませる】
# 以前は8つを1行に横並びにしていました。横に長い行は**目が戻る場所を
# 見失います** ── 見出しと欄が離れ、どこまで入れたのかも数えにくい。
# 縦に積めば、見出しは左に揃い、欄は同じ幅で右に揃うので、
# 「上から順に埋める」だけで済みます。
#
# 分け方は VBA のフォームの囲み(製品情報 / 梱包情報)と同じです ──
# 現場が覚えている塊を崩さない。

# 製品の寸法。**ロット番号から入る**(打ち直すこともできる)
SIZE_FIELDS: tuple[tuple[str, str, str], ...] = (
    ("thickness_mm", "板厚", "mm"),
    ("width_mm", "板幅", "mm"),
    ("length_mm", "板丈", "mm"),
)

# 梱包の形。**ここだけは人が決める**(現物を見ないと分からない)
PACK_FIELDS: tuple[tuple[str, str, str], ...] = (
    ("count", "梱包締枚数", "枚"),
    # 縦バンドは**2本まで**(1本は中央・2本は左右対称)。打つ前に分かるよう
    # 単位の欄に書いておく ── 断られてから知るのでは遅い
    ("vertical_bands", "縦バンド本数", "本(2本まで)"),
    ("horizontal_bands", "横バンド本数", "本"),
    ("pack_height_mm", "梱包高さ(パレット含)", "mm"),
    ("pallet_weight_kg", "パレット+蓋の重量", "kg"),
)

# スピンボタン(▲▼)を付ける欄と、その下限・上限(v4.16.0)。**打つのはそのまま**
#     梱包締枚数 / 縦バンド本数 / 横バンド本数 にスピンボタンを追加し数値入力できるように
#     (直接入力はそのまま)
# 縦バンドは2本まで(上の「本(2本まで)」と同じ)。上限の無い欄は None
SPIN_FIELDS: dict[str, tuple[int, int | None]] = {
    "count": (1, None),
    "vertical_bands": (0, 2),
    "horizontal_bands": (0, None),
}

# ロット・受注から入る、打たない欄。**打つ欄と混ぜない** ──
# 混ざっていると、どれを埋めればよいのかが読めない
PRODUCT_FIELDS: tuple[tuple[str, str], ...] = (
    ("material", "材質"),
    ("temper", "調質"),
    ("usage_code", "用途コード"),
    ("pack_spec_no", "包装仕様"),
    ("unit_weight_kg", "単重(kg)"),
    ("course", "コース"),
    ("customer", "得意先"),
    ("delivery", "納入先"),
    ("sender", "送り先"),
)

# 資材の使用有無 (`MaterialFlags` と同じ名前)
MATERIAL_FLAGS: tuple[tuple[str, str], ...] = (
    ("dunplate", "ダンプレート"),
    ("outer_paper", "外装紙"),
    ("interleaf", "合紙"),
    ("band", "バンド"),
    ("poly_sheet", "ポリシート"),
    ("angle", "縦バンドアングル"),
    ("hardboard", "ハードボード"),
)

# 資材重量マスタでの資材名。VBA `GetUnitMassAndCoefficient` が引く名前
MATERIAL_NAMES: dict[str, str] = {
    "dunplate": "ダンプレート",
    "outer_paper": "外装紙",
    "interleaf": "合紙",
    "poly_sheet": "ポリシート",
    "angle": "縦バンドアングル",
    "hardboard": "ハードボード",
}

# バンドの種別。**名前は `gw_autoselect` が持っている** ── 自動で選ぶ側と
# 画面に並べる側で別々に書くと、片方だけ直したときに選べない種別ができる
BAND_KINDS: tuple[str, ...] = (
    autoselect.BAND_NO_SEAL, autoselect.BAND_WITH_SEAL, autoselect.BAND_PET)

# 「帯鉄」を前に付ける種別 (:func:`_band_material_name`)
_BANDS_WITH_STEEL_PREFIX = frozenset(
    {autoselect.BAND_NO_SEAL, autoselect.BAND_WITH_SEAL})

# 積み形態(山_1 / 山_2 / 山_3)
STACK_PATTERNS: tuple[int, ...] = (1, 2, 3)

# 結果の並び。(識別子, 見出し, 太字で出すか)
#
# **資材ごとの重量を先に、合計を後に。** 足し算の向きと同じ並びにすると、
# 「どれを足してこの数になったのか」が目で追えます。VBA のフォームも
# 資材ごとの欄が上、風袋総重量・GW が下でした。
RESULT_ROWS: tuple[tuple[str, str, bool], ...] = (
    ("ダンプレート", "ダンプレート", False),
    ("外装紙", "外装紙", False),
    ("合紙", "合紙", False),
    ("バンド", "バンド", False),
    ("ポリシート", "ポリシート", False),
    ("縦バンドアングル", "縦バンドアングル", False),
    ("ハードボード", "ハードボード", False),
    ("VCフィルム", "VCフィルム", False),
    ("material_total", "梱包資材重量", True),
    ("tare_weight", "風袋総重量", True),
    ("gross_weight", "GW", True),
)


def _float(value: Any) -> float:
    try:
        return float(str(value).strip() or 0)
    except ValueError:
        return 0.0


def _band_material_name(kind: str) -> str:
    """バンドの資材名。

    VBA の `Select Case` どおり、シール無/シール有のときだけ「帯鉄」を
    前に付ける。PETバンドはそのままの名前でマスタに載っている
    (提出された資材重量マスタで確認済み)。
    """
    if kind in _BANDS_WITH_STEEL_PREFIX:
        return f"帯鉄{kind}"
    return kind


@bp.get("/gw")
def index():
    ctx = work_context.get_context()
    from .entry import current_calculator
    calc = current_calculator()
    return render_template(
        "gw.html",
        size_fields=SIZE_FIELDS,
        pack_fields=PACK_FIELDS,
        spin_fields=SPIN_FIELDS,
        product_fields=PRODUCT_FIELDS,
        material_flags=MATERIAL_FLAGS,
        band_kinds=BAND_KINDS,
        stack_patterns=STACK_PATTERNS,
        # 結果の並び。**画面が勝手に並べない**(資材の順は現場の覚え順)
        result_rows=RESULT_ROWS,
        # ＶＣの品名は**選ぶもの**(VBA `VCCombo設定` が ComboBox1/2 へ
        # 積んでいたのと同じ一覧)。打たせると、マスタに無い綴りが通って
        # しまい「登録されていないVCです」で計算が止まります
        vc_names=_vc_names(),
        **shell.shell_context("gw", ribbon=ctx.ribbon(calc)))


def _vc_names() -> list[str]:
    """ＶＣの品名の一覧(VBA `VCCombo設定` → ComboBox1/2)。

    **読めなくても画面は出します。** 一覧が空なら選ぶ欄は打つ欄へ
    落とします(`gw.html`)── マスタへ届かない日に、GW計算そのものが
    できなくなるほうが困ります。
    """
    try:
        return gw_master.list_vc_product_names(gw_master.load_vc_rate_rows())
    except Exception:                                # noqa: BLE001 - 画面は出す
        log.exception("VC重量マスタの品名一覧を読めませんでした")
        return []


def _dimensions(payload: dict) -> gw.PackingDimensions:
    pattern = int(_float(payload.get("stack_pattern")) or 1)
    return gw.PackingDimensions(
        thickness_mm=_float(payload.get("thickness_mm")),
        width_mm=_float(payload.get("width_mm")),
        length_mm=_float(payload.get("length_mm")),
        count=_float(payload.get("count")),
        vertical_bands=_float(payload.get("vertical_bands")),
        horizontal_bands=_float(payload.get("horizontal_bands")),
        pack_height_mm=_float(payload.get("pack_height_mm")),
        pallet_weight_kg=_float(payload.get("pallet_weight_kg")),
        stack_pattern=pattern if pattern in STACK_PATTERNS else 1,
    )


def _master_names(band_kind: str) -> dict[str, str]:
    """資材ごとに、マスタで引く名前。**断りの文言もこの名前で言う。**"""
    return {**MATERIAL_NAMES, "band": _band_material_name(band_kind)}


def _rates(band_kind: str) -> Optional[gw.MaterialRates]:
    """資材重量マスタから各資材の単位質量・係数を引く。

    **マスタが読めなければ `None`**(VBA ``MsgBox "資材重量なし"`` で
    止めていたところ)。以前は全部0で計算を続けていましたが、それだと
    パレット重量だけの**軽いGW**が出ます ── 抜けたことは内訳の注記にしか
    出ませんでした。呼ぶ側が断ります。
    """
    rows = gw_master.load_material_rate_rows()
    if not rows:
        return None
    return gw.MaterialRates(**{
        key: gw_master.lookup_material_rate(rows, name)
        for key, name in _master_names(band_kind).items()
    })


def _refuse(problems: list[gw.InputProblem]):
    """断りを1つの応答にまとめる。

    **全部返す。** VBA は1つ見つけるたびに `MsgBox` して止めていたので、
    3か所間違っていると3回押し直すことになりました。画面は `problems` を
    使って、間違っている欄すべてに印を付けられます。
    """
    body = error_body("bad_input", problems[0].message,
                      field=problems[0].field)
    body["problems"] = [{"field": p.field, "message": p.message}
                        for p in problems]
    return jsonify(body), 422


@dataclass
class _Computed:
    """1回ぶんの計算結果ひとそろい。**画面と紙が同じものを見るための束。**

    計算を2回書くと、いつか画面と紙で数字が違う日が来ます。押したときに
    出るものと、刷ったときに出るものは、ここから作られた同じ値です。
    """

    payload: dict
    dims: gw.PackingDimensions
    flags: gw.MaterialFlags
    rates: gw.MaterialRates
    weights: gw.MaterialWeights
    selection: gw.VcFilmSelection
    vc_a: gw.MaterialRate
    vc_b: gw.MaterialRate
    vc_name_a: str
    vc_name_b: str
    band_kind: str
    use_combined_load: bool
    combined_load_kg: float
    input_weight_kg: Optional[float]
    tare_weight_kg: float
    gross_weight_kg: Optional[float]


def _truthy(value: Any) -> bool:
    """チェックが入っているか。

    JSON からは本物の真偽値で、クエリからは文字で来ます。`bool("0")` は
    True なので、**文字のときは中身を見ます** ── ここを素通りさせると、
    「VCを使わない」で開いた紙にVCが乗ります。
    """
    if isinstance(value, bool):
        return value
    return str(value or "").strip().lower() not in ("", "0", "false", "off", "no")


def _compute(payload: dict):
    """入力ひとそろいから計算する。**断るなら (None, 応答) を返す。**

    `POST /api/gw/calculate` と印刷の両方がここを通ります ── 同じ入力
    から同じ数字が出ることが、紙と画面が食い違わない唯一の担保です。
    """
    dims = _dimensions(payload)

    selection = gw.VcFilmSelection(
        use_vc=_truthy(payload.get("use_vc")),
        use_side_a=_truthy(payload.get("vc_side_a")),
        use_side_b=_truthy(payload.get("vc_side_b")),
    )
    vc_name_a = str(payload.get("vc_name_a", ""))
    vc_name_b = str(payload.get("vc_name_b", ""))
    use_combined = _truthy(payload.get("use_combined_load"))
    combined = _float(payload.get("combined_load_kg"))

    problems = gw.validate_inputs(
        dims,
        pack_spec_no=str(payload.get("pack_spec_no", "")),
        use_combined_load=use_combined,
        combined_load_kg=combined,
        vc_selection=selection,
        vc_name_a=vc_name_a, vc_name_b=vc_name_b)
    if problems:
        return None, _refuse(problems)

    raw_flags = payload.get("flags") or {}
    flags = gw.MaterialFlags(**{
        key: _truthy(raw_flags.get(key, True)) for key, _ in MATERIAL_FLAGS})

    band_kind = str(payload.get("band_kind", BAND_KINDS[0]))
    rates = _rates(band_kind)
    if rates is None:
        return None, _refuse([gw.InputProblem("materials", gw.MASTER_UNREADABLE)])
    # **使う資材がマスタに無い**なら止める(VCの「登録されていないVCです」と
    # 同じ理由 ── そのぶん軽いGWが出る)
    missing = gw.unregistered_material_problems(
        flags, rates, _master_names(band_kind))
    if missing:
        return None, _refuse(missing)

    # VCフィルムは品名で別マスタ(VC重量)を引く。選ばれていなければ0
    vc_rows = gw_master.load_vc_rate_rows()
    vc_a = gw_master.lookup_vc_rate(vc_rows, vc_name_a)
    vc_b = gw_master.lookup_vc_rate(vc_rows, vc_name_b)

    weights = gw.compute_material_weights(dims, flags, rates, selection, vc_a, vc_b)

    # **VCを使うと言ったのに0**。マスタに無い品名なので、そのまま通すと
    # VCぶんが抜けたGWになる(VBA「登録されていないVCです」)
    unknown_vc = gw.unregistered_vc_problem(selection, weights.vc_film)
    if unknown_vc is not None:
        return None, _refuse([unknown_vc])

    tare = gw.total_packing_weight(
        dims.pallet_weight_kg, weights.total,
        combined if use_combined else 0.0)
    input_weight = _float(payload.get("input_weight_kg")) or None
    gross = gw.gross_weight(tare, input_weight)

    return _Computed(
        payload=payload, dims=dims, flags=flags, rates=rates, weights=weights,
        selection=selection, vc_a=vc_a, vc_b=vc_b,
        vc_name_a=vc_name_a, vc_name_b=vc_name_b, band_kind=band_kind,
        use_combined_load=use_combined, combined_load_kg=combined,
        input_weight_kg=input_weight,
        tare_weight_kg=tare, gross_weight_kg=gross), None


@bp.post("/api/gw/calculate")
def calculate():
    """資材重量を計算する(`NewGW` 相当)。

    計算できない入力は **422 で断る** ── 形は正しいが業務として計算
    できない。判断は `logic/gw_calculation.validate_inputs` が持ち、
    ここは HTTP に写すだけ。
    """
    payload = request.get_json(silent=True) or {}
    computed, refusal = _compute(payload)
    if computed is None:
        return refusal

    dims, flags, rates = computed.dims, computed.flags, computed.rates
    weights, selection = computed.weights, computed.selection
    vc_a, vc_b = computed.vc_a, computed.vc_b
    vc_name_a, vc_name_b = computed.vc_name_a, computed.vc_name_b
    band_kind = computed.band_kind
    combined, use_combined = computed.combined_load_kg, computed.use_combined_load
    tare, gross = computed.tare_weight_kg, computed.gross_weight_kg

    # **どう出したのか**を一緒に返す。数字だけでは確かめようがない
    # (`presenters/gw_breakdown.py`)
    from nippou.presenters import gw_breakdown

    breakdown = gw_breakdown.build(
        dims, flags, rates, weights,
        vc_selection=selection, vc_a_rate=vc_a, vc_b_rate=vc_b,
        vc_name_a=vc_name_a, vc_name_b=vc_name_b,
        band_kind=band_kind, band_material=_band_material_name(band_kind),
        combined_load_kg=combined, use_combined_load=use_combined,
        input_weight_kg=computed.input_weight_kg,
        tare_weight_kg=tare, gross_weight_kg=gross,
        product={key: payload.get(key, "")
                 for key in ("lot_no", "order_no", "pack_spec_no")})

    return jsonify({
        "stack_count": gw.stack_count(dims),
        "breakdown": breakdown.as_dict(),
        "breakdown_text": breakdown.as_text(),
        "weights": {
            "ダンプレート": round(weights.dunplate, 2),
            "外装紙": round(weights.outer_paper, 2),
            "合紙": round(weights.interleaf, 2),
            "バンド": round(weights.band, 2),
            "ポリシート": round(weights.poly_sheet, 2),
            "縦バンドアングル": round(weights.angle, 2),
            "ハードボード": round(weights.hardboard, 2),
            "VCフィルム": round(weights.vc_film, 2),
        },
        "material_total": round(weights.total, 2),
        "tare_weight": round(tare, 2),
        # Aインプット重量が空/0のとき、VBAは GW欄そのものを隠していた。
        # ここでは null で「出さない」を表す
        "gross_weight": None if gross is None else round(gross, 2),
        # 梱包数ごとの重量(1〜100梱包)。「この梱包を N 個作るなら資材は
        # いくつ要るか」に答える表(VBA `RangePaste` → メイン C25:L124)
        "per_pack": {
            "columns": list(gw.PER_PACK_COLUMNS),
            "rows": [[round(v, 2) for v in row]
                     for row in gw.per_pack_table(weights, dims.pallet_weight_kg)],
        },
        # 梱包図(外形)。**輸出梱包の完成確認用**なので、中身は描かない
        # (`logic/packing_figure.py`)。計算は通っても図は描けないことが
        # あるので、描けない理由も一緒に返す
        "figure": packing_figure.build(
            thickness_mm=dims.thickness_mm, width_mm=dims.width_mm,
            length_mm=dims.length_mm, count=dims.count,
            horizontal_bands=dims.horizontal_bands,
            vertical_bands=dims.vertical_bands,
            pack_height_mm=dims.pack_height_mm,
            columns=dims.stack_pattern,
            use_angle=flags.angle, band_kind=band_kind).as_dict(),
        "message": "計算しました",
    })


@bp.post("/api/gw/lot")
def search_lot():
    """LotNo で仕掛から引く(`寸法表示` 相当)。**受注番号まで決めて返す。**

    以前は SIKALOT から寸法を拾うだけで、オーダーNo は人が別の欄に
    打ち直していました。VBA のフォームも「LotNo入力後にオーダーNoを
    選択してください」と刷ってあるとおり、**ロット番号を打てば受注番号は
    決まっている**もので、打ち直させるものではありません。

        SIKALOT  ﾛｯﾄ番号  → 材質 / 調質 / 用途コード / 板厚幅丈 / コース
              ↓ (ﾛｯﾄ番号)
        SIKAHIKI ﾛｯﾄ番号  → 受注番号(複数ありうる)  ← 唯一の橋
              ↓ (受注番号)
        SIKAODR  受注番号 → ＶＣ / 合紙 / 単重 / 包装仕様NO

    引当が1件ならそこまで辿って返します。複数のときは**受注番号の一覧
    だけ**返し、画面はコンボボックスに並べて選ばせます(選んだら
    `/api/gw/order` を呼び直す)。**選ぶまで受注側の値は決めません** ──
    どれを選ぶかで ＶＣ も合紙も単重も変わるからです。
    """
    # **画面の直しは貼り付けやIMEで通り抜ける。** サーバでも同じ形に
    # 直してから引きます(日報入力のLOT欄と同じ `input_rules.normalize`)
    from nippou.logic import input_rules

    lot_no = input_rules.normalize(
        "LOT", str((request.get_json(silent=True) or {}).get("lot_no", "")))
    if not lot_no:
        return jsonify(error_body("empty", "LotNoを入力してください",
                                  field="lot_no")), 400
    info = gw_master.search_lot(lot_no)
    if info is None:
        # **見つからないのは失敗ではない。** VBA も「データなし 手入力
        # よろしく」と案内して続行させていた
        return jsonify({"found": False, "allocations": [],
                        "message": "該当データがありません。手入力してください。"})

    allocations = gw_master.search_allocations(lot_no)
    # 製品の寸法。**Gコース(設計_設備ｺｰｽ に GCT/GFS/GSS)なら BOX最終実績**(v4.17.0)
    # ── 日報入力と同じ寸法(`logic/g_course`)。画面はこの3つを欄へ入れ、
    # Gコースなら寸法の上にそう書く(製造の寸法も並べる)
    thickness, width, length = g_course.product_size(info)
    found_g = g_course.from_lot(info)
    body: dict[str, Any] = {
        "found": True, "lot": asdict(info),
        "size": {"thickness_mm": thickness, "width_mm": width, "length_mm": length},
        "g_course": found_g.as_dict() if found_g else None,
        "allocations": [asdict(a) | {"label": a.label} for a in allocations],
        "message": "",
    }
    if not allocations:
        # 受注番号が分からないので、ＶＣ・合紙・単重は手で決めることになる
        body["message"] = (
            "SIKAHIKI にこのロットの引当がありません。"
            "オーダーNoが分からないので、ＶＣ・合紙・単重は手で入れてください。")
        return jsonify(body)

    if len(allocations) == 1:
        # 1件しかないなら選ばせる意味が無い。そのまま受注まで辿る
        body.update(_order_body(allocations[0].order_no, payload={}))
    else:
        body["message"] = (
            f"引当が{len(allocations)}件あります。オーダーNoを選んでください。")
    return jsonify(body)


def _order_body(order_no: str, payload: dict) -> dict[str, Any]:
    """受注1件から、画面へ返すものを組み立てる。

    `/api/gw/lot`(引当が1件のとき)と `/api/gw/order`(コンボで選び直した
    とき)の**両方が同じものを返す**ようにここへ置きます ── 別々に組むと、
    片方だけ自動選択を忘れる、という食い違いが起きます。
    """
    info = gw_master.search_order(order_no)
    if info is None:
        return {"order": None, "auto": None,
                "message": f"SIKAODR に受注 {order_no} がありません。"
                           "ＶＣ・合紙・単重は手で入れてください。"}

    # **合紙は SIKAODR の「合紙」列から。** `select_interleaf` が
    # "1" のときだけ有にする(`logic/gw_autoselect`)
    auto = autoselect.select_all(
        vc_front=info.vc_front, vc_back=info.vc_back,
        interleaf_flag=info.interleaf_flag,
        customer=info.customer, delivery=info.delivery,
        pack_spec_no=info.pack_spec_no,
        vertical_bands=_float(payload.get("vertical_bands")) or None)
    return {"order": asdict(info), "auto": asdict(auto), "message": ""}


@bp.post("/api/gw/order")
def search_order():
    """オーダーNo で引き、**梱包仕様まで決めて返す**。

    VBA は `Hiki展開` の末尾で `VC選択` → `合紙選択` → `バンド選択` を
    呼び、その場でチェックを付け直していました。VC を使うか、合紙を挟むか、
    帯鉄かPETか、縦バンドは何本か ── どれも取引先・納入先・包装仕様で
    決まっているので、毎回人が選ぶ必要がありません。

    **決めるだけで、押しません。** `auto.decided` に「自動で決まった項目」
    を入れて返すので、画面はそれを示したうえで人が変えられます
    (VBA も赤色で示していただけで、変更はできました)。
    """
    payload = request.get_json(silent=True) or {}
    order_no = str(payload.get("order_no", "")).strip()
    if not order_no:
        return jsonify(error_body("empty", "オーダーNoを選んでください",
                                  field="order_no")), 400
    body = _order_body(order_no, payload)
    body["found"] = body["order"] is not None
    return jsonify(body)


# ======================================================================
# 印刷 (VBA `UFGW.CommandButton3_Click` → `印刷()` / `印刷2()`)
#
# VBA は「メイン」シートを開いて、印刷範囲を変えた2つの Sub のどちらかを
# 走らせていました。Web版に座標はないので、**その範囲が何を見せていたか**
# だけを残します(`reporting/gw_print.py` が紙を組み立てます)。
#
# 【紙は1種類。表は刷らず、梱包数を聞く】
# v3.21.0 ではVBAの印刷範囲そのままに「2つから選ぶ」形にしましたが、
# **紙で要るのは1行だけ**でした。100行の表を刷っても読むのは「いま作る
# 梱包数」の行1つで、残りは探す手間になります。押す前に梱包数を聞いて、
# その行だけを計算結果の紙に載せます。
#
# 【GET にする理由】
# 新しい窓で開いて Ctrl+P で刷る、という日報の紙(`/report/nippou`)と
# 同じ道にします。入力ひとそろいがそのまま鍵になるので、**その紙の URL を
# もう一度開けば同じ紙が出ます** ── 計算結果はどこにも保存していないので、
# 鍵は入力そのものになります。
# ======================================================================

#: 資材の並び。**紙の順は画面の順と同じ**(`RESULT_ROWS`)にするので、
#: 画面と紙を突き合わせるときに目が迷いません
_PRINT_MATERIALS: tuple[tuple[str, str, str], ...] = (
    ("dunplate", "ダンプレート", "dunplate"),
    ("outer_paper", "外装紙", "outer_paper"),
    ("interleaf", "合紙", "interleaf"),
    ("band", "バンド", "band"),
    ("poly_sheet", "ポリシート", "poly_sheet"),
    ("angle", "縦バンドアングル", "angle"),
    ("hardboard", "ハードボード", "hardboard"),
)


def _payload_from_args(args: dict) -> dict:
    """クエリ文字列を、`/api/gw/calculate` と同じ形にする。

    **計算に渡す形を1つに保つ**ためだけの変換です。資材のチェックは
    `flags=dunplate,band,...` の形で ON のものだけを並べます ── 7つを
    別々の欄にすると URL が読めなくなります。
    """
    payload = dict(args)
    names = {key for key, _ in MATERIAL_FLAGS}
    raw = str(args.get("flags", "")).strip()
    if "flags" in args:
        chosen = {v.strip() for v in raw.split(",") if v.strip()}
        payload["flags"] = {key: key in chosen for key in names}
    else:
        # 指定が無ければ全部使う(画面の既定と同じ)
        payload["flags"] = {key: True for key in names}
    return payload


def _pack_rows(computed: _Computed, packs: int) -> list[gw_print.Row]:
    """「◯梱包ぶん」の段。**VBAの表の該当行を、そのまま1行ずつに。**

    出すのは `logic/gw_calculation.per_pack_table` が作る10項目と同じ
    もの(8資材 + 資材計 + 風袋総重量)です ── 掛け算をここで書き直すと、
    画面の表と紙で数字がずれる余地ができます。

    **使わない資材は、上の段と同じく薄く出します。** 上で「－」なのに
    下で「0.00」だと、同じ紙の中で違うことを言っているように見えます。
    """
    if packs <= 1:
        return []
    # **その行だけ**を出す(表を丸ごと作らない)。手間が梱包数に比例すると、
    # 安全が「上限で丸める」の1枚だけに乗ることになる
    values = gw.per_pack_row(computed.weights,
                             computed.dims.pallet_weight_kg, packs)
    strong = {"資材計", "風袋総重量"}
    # 資材名 → 使うかどうか。合計の2つは常に出す
    used_of = {label: getattr(computed.flags, flag)
               for _, label, flag in _PRINT_MATERIALS}
    used_of["VCフィルム"] = computed.selection.use_vc
    return [gw_print.row(name, value, "kg", strong=name in strong,
                         used=used_of.get(name, True))
            for name, value in zip(gw.PER_PACK_COLUMNS, values)]


def _print_data(computed: _Computed, packs: int = 1) -> gw_print.GwPrintData:
    """計算結果を紙のかたちへ。**数字はここで作り直しません。**

    VBA の「メイン」シートと同じ並びにします ── 左に入れたもの
    (製品情報 D6:E20)、右に出たもの(資材と合計 G5:J20)。
    """
    row = gw_print.row
    payload, dims, weights = computed.payload, computed.dims, computed.weights
    flags = computed.flags

    product = [row(label, str(payload.get(key, "")).strip() or "－", digits=None)
               for key, label in (("lot_no", "LotNo"), ("order_no", "オーダーNo"))
               ] + [
        row(label, str(payload.get(key, "")).strip() or "－", digits=None)
        for key, label in PRODUCT_FIELDS]

    # Gコースのロット(v4.17.0)。**紙にも書く** ── 寸法が SIKALOT の製造の寸法と
    # 違う理由が、紙だけ見た人にも分かるように
    g_mark = g_course.row_mark(str(payload.get("g_course", "")))
    g_sub = g_mark["title"] if g_mark else ""
    dimensions = [
        row("板厚", dims.thickness_mm, "mm", sub=g_sub),
        row("板幅", dims.width_mm, "mm"),
        row("板丈", dims.length_mm, "mm"),
        row("梱包締枚数", dims.count, "枚", digits=0),
        row("縦バンド本数", dims.vertical_bands, "本", digits=0),
        row("横バンド本数", dims.horizontal_bands, "本", digits=0),
        row("梱包高さ(パレット含)", dims.pack_height_mm, "mm"),
        row("パレット+蓋の重量", dims.pallet_weight_kg, "kg"),
        row("積み形態", f"{dims.stack_pattern}山積み", digits=None),
        row("積み枚数", gw.stack_count(dims), "枚", digits=0,
            sub="1山なら梱包締枚数そのまま"),
    ]

    materials = []
    for key, label, flag in _PRINT_MATERIALS:
        used = getattr(flags, flag)
        sub = computed.band_kind if key == "band" else ""
        materials.append(row(label, getattr(weights, key) if used else None,
                             "kg" if used else "", used=used, sub=sub))
    # VCフィルムだけはチェックではなく「使う/使わない」と品名で決まる
    vc_used = computed.selection.use_vc
    vc_sub = " / ".join(filter(None, [
        f"上面 {computed.vc_name_a}" if computed.selection.use_side_a else "",
        f"下面 {computed.vc_name_b}" if computed.selection.use_side_b else "",
    ]))
    materials.append(row("VCフィルム", weights.vc_film if vc_used else None,
                         "kg" if vc_used else "", used=vc_used, sub=vc_sub))

    totals = [row("梱包資材重量", weights.total, "kg", strong=True)]
    if computed.use_combined_load:
        totals.append(row("積合せ重量", computed.combined_load_kg, "kg"))
    totals.append(row("風袋総重量", computed.tare_weight_kg, "kg", strong=True))
    # Aインプット重量が空/0のとき、VBA は GW の欄そのものを隠していた
    if computed.input_weight_kg:
        totals.append(row("Aインプット重量", computed.input_weight_kg, "kg"))
        totals.append(row("GW(総重量)", computed.gross_weight_kg, "kg",
                          strong=True))
    else:
        totals.append(row("GW(総重量)", "Aインプット重量が未入力", digits=None,
                          used=False))

    lot = str(payload.get("lot_no", "")).strip()
    order = str(payload.get("order_no", "")).strip()
    subject = " / ".join(filter(None, [
        f"LotNo {lot}" if lot else "", f"オーダーNo {order}" if order else "",
        f"Gコース({g_mark['course']})" if g_mark else ""]))

    return gw_print.GwPrintData(
        product=product, dimensions=dimensions, materials=materials,
        totals=totals, pack_count=packs,
        pack_rows=_pack_rows(computed, packs),
        subject=subject)


@bp.get("/report/gw")
def print_gw():
    """梱包資材重量計算の紙(A4 横1枚)。

        /report/gw?packs=12&print=1&<計算と同じ入力>

    `packs` は**いま作る梱包数**です。入れるとその梱包数ぶんの資材重量が
    計算結果の下に一段入ります(1なら入りません ── 上の数字と同じなので)。

    入力は `/api/gw/calculate` と同じ名前で渡します ── 計算結果は
    どこにも保存していないので、**入力そのものが鍵**です。

    断るときも HTML を返します。新しい窓で開く経路なので、JSON の断りを
    返すと押した人には白紙が出るだけになります。
    """
    args = request.args.to_dict(flat=True)
    packs = gw_print.normalize_packs(args.get("packs"))
    # 画面の「印刷」から開いたとき。**そのまま印刷の画面を出す**
    auto_print = args.pop("print", "") == "1"

    computed, refusal = _compute(_payload_from_args(args))
    if computed is None:
        # 断りの中身は JSON なので、文言だけ取り出して紙に出す
        body = refusal[0].get_json() if isinstance(refusal, tuple) else {}
        message = (body.get("error", {}).get("message")
                   or "入力が正しくないため計算できません。")
        return Response(gw_print.build_missing_html(
            f"{message} 画面に戻って直してから、もう一度開いてください。"),
            mimetype="text/html")

    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    log_button_click("gw_print", extra=f"{packs}梱包")
    return Response(
        gw_print.build_html(_print_data(computed, packs), generated_at=now,
                            auto_print=auto_print),
        mimetype="text/html")
