"""Gコースのロット ── 寸法は BOX最終実績、そして**Gコースだったことを見せる** (v4.16.0 / v4.17.0)

    設計_設備ｺｰｽに GCT GFS GSS のどれかが含まれていた場合
    BOX最終実績_板厚、BOX最終実績_板幅、BOX最終実績_板丈 が寸法になるようにしてください
    GWも同様にしてください、ただし日報入力もGWもGのコースがあったことはわかるようにしてください

【寸法】
Gコース(`設計_設備ｺｰｽ` に GCT / GFS / GSS のどれかを含む。部分一致)のロットは、
製品の寸法を **BOX最終実績_板厚 × 板幅 × 板丈** にします。日報の製品寸法も、GW の
計算も、包装仕様の注意(寸法の条件)も同じ寸法を見ます ── 1か所でだけ違う寸法を
使うと、紙と計算が食い違います。

BOX最終実績の寸法が3つとも 0(列の無い古い写し・最終工程がまだ無い)なら、製造の
寸法のままにします ── 0×0×0 で計算させないため。そのときも**Gコースだったことは
言います**(なぜ製造の寸法なのかが分かるように)。

【見せ方】
製造の寸法と違う寸法が入るので、黙って入れると「SIKALOT と違う」と思われます。
日報入力では寸法の欄に印(行に控えて、開き直しても残る)、GW では寸法の上に一言。
文言はここが持ち、画面は写すだけです。

【行への控え】
日報入力の行は `box_course` に控えます(引当番号と同じく**この端末の中だけ** ──
紙にも共有の日報管理にも欄はありません)。値は `GSS`(BOX最終実績の寸法を使った)か
`GSS/製造`(BOX最終実績の寸法が無く、製造の寸法のまま)。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional

#: Gコース(部分一致)。python-web-tools `lot_service.BOX_COURSES` と同じ3つ。
#: 外注の印(`etc_marks.OUTSOURCE_COURSES`)も同じ3つを見ています
COURSES: tuple[str, ...] = ("GCT", "GFS", "GSS")

#: 日報入力の行に控える欄(`presenters/entry.EXTRA_FAMILIES`・`daily_detail.box_course`)
ROW_FAMILY = "box_course"

#: 行の控えで「製造の寸法のまま」を表す後ろ書き
MADE_SUFFIX = "/製造"

Size = tuple[float, float, float]


def find_course(course: str) -> str:
    """設計_設備ｺｰｽ に含まれる Gコース(GCT / GFS / GSS)。無ければ空。"""
    text = course or ""
    return next((c for c in COURSES if c in text), "")


def size_text(size: Size) -> str:
    """厚×幅×丈(日報の製品寸法と同じ書き方 `0.000×0.0×0.0`)。"""
    t, w, length = size
    return f"{t:.3f}×{w:.1f}×{length:.1f}"


@dataclass(frozen=True)
class GCourse:
    """Gコースのロット1本。"""

    course: str           # GCT / GFS / GSS
    used_box: bool        # BOX最終実績の寸法にしたか
    box: Size             # BOX最終実績の寸法
    made: Size            # 製造の寸法

    @property
    def label(self) -> str:
        return f"Gコース({self.course})"

    @property
    def size(self) -> Size:
        """使う寸法。"""
        return self.box if self.used_box else self.made

    @property
    def stored(self) -> str:
        """日報入力の行に控える値(`box_course`)。"""
        return self.course if self.used_box else f"{self.course}{MADE_SUFFIX}"

    @property
    def message(self) -> str:
        """引いたときに出す一言。**製造の寸法も並べる**(SIKALOT と見比べられるように)。"""
        if self.used_box:
            return (f"{self.label}のロットです。寸法は BOX最終実績 {size_text(self.box)}"
                    f"(製造 {size_text(self.made)})")
        return (f"{self.label}のロットです。BOX最終実績の寸法が無いので、"
                f"製造の寸法 {size_text(self.made)} のままです")

    def as_dict(self) -> dict[str, Any]:
        return {"course": self.course, "label": self.label, "used_box": self.used_box,
                "box": size_text(self.box), "made": size_text(self.made),
                "stored": self.stored, "message": self.message}


def detect(course: str, *, made: Size, box: Size) -> Optional[GCourse]:
    """Gコースなら `GCourse`、ちがえば None。"""
    found = find_course(course)
    if not found:
        return None
    box = tuple(float(v or 0) for v in box)       # type: ignore[assignment]
    made = tuple(float(v or 0) for v in made)     # type: ignore[assignment]
    return GCourse(course=found, used_box=any(box), box=box, made=made)


def from_lot(lot: Any) -> Optional[GCourse]:
    """`gw_master.LotInfo` から。**属性だけ見る**(試験は軽い作り物を渡せる)。"""
    return detect(
        getattr(lot, "course", "") or "",
        made=(getattr(lot, "thickness_mm", 0.0), getattr(lot, "width_mm", 0.0),
              getattr(lot, "length_mm", 0.0)),
        box=(getattr(lot, "box_thickness_mm", 0.0), getattr(lot, "box_width_mm", 0.0),
             getattr(lot, "box_length_mm", 0.0)))


def product_size(lot: Any) -> Size:
    """そのロットの製品寸法(厚・幅・丈)。Gコースなら BOX最終実績(無ければ製造)。"""
    found = from_lot(lot)
    if found is not None:
        return found.size
    return (float(getattr(lot, "thickness_mm", 0.0) or 0),
            float(getattr(lot, "width_mm", 0.0) or 0),
            float(getattr(lot, "length_mm", 0.0) or 0))


def row_mark(stored: str) -> Optional[dict[str, str]]:
    """日報入力の行の印(寸法の欄に出す)。控えが空なら None。"""
    text = (stored or "").strip()
    if not text:
        return None
    made = text.endswith(MADE_SUFFIX)
    course = text[:-len(MADE_SUFFIX)] if made else text
    detail = ("BOX最終実績の寸法が無いので、製造の寸法のまま" if made
              else "寸法は BOX最終実績")
    return {"text": "G", "course": course,
            "title": f"Gコース({course})のロット ── {detail}"}
