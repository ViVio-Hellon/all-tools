"""早見表のマスをまとめて作る(vc-calculator `master_admin.make_grid` の移植)

早見表に枠(品種)を1つ足すと、マス(内径 × 肉厚)を1つずつ「早見表値」に
入れることになります。十数マスを1行ずつ打つのは手間で、打ち漏れも出ます。
内径と肉厚の並びを入れれば、そのかけ合わせを**1つの取引で**足します。

- すでにあるマスはそのまま(長さも変えない)。並びに無いマスも消さない
- 計算品種のある枠は、長さを入れない(VC厚 から式で出す)
- **計算品種の無い枠(固定値)は、VC厚 を1つ決めれば長さを式で出して入れる**
  (v3.96.0)── VC品種 の VC厚 を借りるか、VC厚 を直に入れる。丸めは早見表と
  同じ(アプリ設定「早見表の丸め」)。決めなければ長さは空欄のまま(前と同じ)
- すでにあるマスでも**長さが空なら入れる**(`fill_empty`、既定で入れる)。入って
  いる長さは、置き換えると決めたとき(`overwrite`)だけ書き換える
- 変更履歴には1行でまとめて残し、更新番号を上げる(vc-calculator の端末にも効く)

    計算してくれればいいのでは? マスタの早見表値を編集しないとだめですか?

v3.97.0 から、品種(枠)そのものもここで**1回で**足す・消す(下の節):

- `add_block` … VC品種(新しくも可)・枠・マスを1つの取引で。長さは式か固定値を選ぶ
- `delete_block` … 枠をマスごと / `delete_cells` … 内径の行・肉厚の列
- `set_source` … 式 ⇔ 固定値 の切り替え(どちらなのかを画面がいつも言う)
- `delete_product` … VC品種を、使っている枠・マス・内径の選択肢ごと

**直すのは管理者だけ**(呼ぶ側 `app/routes/vc.py` が確かめる)。
"""
from __future__ import annotations

import sqlite3
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..logging_setup import get_logger
from . import db, masters
from .db import quote

log = get_logger("vc.grid")

GRID_MAX_INSIDES = 10
GRID_MAX_THICKNESSES = 40
GRID_LIMIT_MM = 1000.0

#: 直に入れる VC厚 の上限(mm)。フィルムの厚みなので 1mm を超えることは無い ──
#: 桁を打ち間違えた値(「10」= 0.10 のつもり)で長さを書き込まない
VCATU_MAX_MM = 1.0

#: **μm でも入れられる**(v4.3.0)。
#:
#:     VC厚の単位はmm単位ではない ミクロンなのでは
#:
#: マスタ(VC品種 の VC厚)と式は **mm** のままです(0.10 / 0.08 / 0.06 / 0.13 =
#: 100 / 80 / 60 / 130μm。vc-calculator とマスタを分け合うので単位は変えない)。
#: 現場ではフィルムの厚みを「100ミクロン」と呼ぶので、入れるときだけ μm も
#: 受け取り、mm に直して使います:
#:
#:     0 < 値 ≤ 1        … mm(0.10)
#:     20 ≤ 値 ≤ 1000    … μm(100 → 0.10mm)
#:     そのあいだ(1〜20)… **断る**。13 が「0.13mm」の打ち間違いか「13μm」か
#:                           分からない ── 黙って10倍違う長さを書き込まない
VCATU_MIN_UM = 20.0
VCATU_MAX_UM = 1000.0


def format_vcatu(mm: float) -> str:
    """VC厚を**両方の単位で**。``0.1mm(100μm)``。"""
    return f"{mm:g}mm({mm * 1000:g}μm)"

#: 固定値の枠の長さをどう入れるか
LENGTH_NONE = "none"           # 入れない(マスタ管理で1マスずつ)
LENGTH_PRODUCT = "product"     # VC品種 の VC厚 で計算する
LENGTH_VALUE = "value"         # VC厚 を直に入れて計算する
LENGTH_MODES = (LENGTH_NONE, LENGTH_PRODUCT, LENGTH_VALUE)

REFUSE_BAD_VALUE = "bad_value"
REFUSE_LOCKED = "db_locked"
REFUSE_NO_SOURCE = "no_source"
REFUSE_WRITE_FAILED = "write_failed"


@dataclass
class Result:
    ok: bool = True
    message: str = ""
    reason: str = ""
    added: int = 0
    #: 長さを入れた(空だったマスに)/ 置き換えた マスの数
    filled: int = 0
    replaced: int = 0


def parse_numbers(text: str, label: str, most: int) -> tuple[list[float], str]:
    """「5, 8, 10」「5 8 10」「５、８」などを数の並びにする(同じ数は1つ・小さい順)。"""
    raw = unicodedata.normalize("NFKC", str(text or ""))
    for sep in ("、", ",", "，", ";", "\n", "\t", "/"):
        raw = raw.replace(sep, " ")
    words = [w for w in raw.split(" ") if w]
    if not words:
        return [], f"{label}を1つ以上入れてください(例: 5, 8, 10)。"
    out: set[float] = set()
    for w in words:
        try:
            v = float(w)
        except ValueError:
            return [], f"{label}に数でないものがあります: {w}"
        if not (0 < v <= GRID_LIMIT_MM) or v != v:
            return [], f"{label}は 0 より大きく {GRID_LIMIT_MM:g} 以下の mm で入れてください: {w}"
        out.add(v)
    if len(out) > most:
        return [], f"{label}は一度に {most} 個までです(いま {len(out)} 個)。"
    return sorted(out), ""


def quick_blocks(path: Path) -> list[dict[str, Any]]:
    """早見表の枠の一覧: 枠ごとに、長さの出し方と、いまある内径・肉厚・マスの数。

    固定値の枠には**長さが空のマスの数**も添える(「入れてください」と言うため)。
    `kind` は人に見せる長さの出し方(式か固定値か)── **固定値になっている理由も言う。**
    """
    from .. import source_db

    q = quote
    out = []
    with source_db.open_source(path) as conn:
        for r in conn.execute(
                f"SELECT b.品種名, b.計算品種, b.業者名, b.枠色, p.VC厚"
                f" FROM {q('早見表ブロック')} b"
                f" LEFT JOIN {q('VC品種')} p ON p.品種名 = b.計算品種"
                " ORDER BY b.表示順, b.rowid"):
            cells = conn.execute(f"SELECT 内径, 肉厚, 長さ FROM {q('早見表値')}"
                                 " WHERE 品種名 = ?", [r[0]]).fetchall()
            product = str(r[1] or "")
            vcatu = "" if r[4] is None else f"{float(r[4]):g}"
            if product:
                shown = format_vcatu(float(r[4])) if r[4] is not None else "?mm"
                kind = f"式: VC品種「{product}」の VC厚 {shown} から出す"
            else:
                kind = "固定値: マスに入れた長さをそのまま出す(VC品種と結びついていない)"
            out.append({"name": str(r[0]), "product": product, "vcatu": vcatu,
                        "vendor": str(r[2] or ""), "tone": str(r[3] or ""), "kind": kind,
                        "insides": [f"{v:g}" for v in sorted({float(c[0]) for c in cells})],
                        "thicknesses": [f"{v:g}" for v in sorted({float(c[1]) for c in cells})],
                        "cells": len(cells),
                        # 行・列を消すときに「マス N を消します」と言うため
                        "inside_cells": _count_by(cells, 0),
                        "thickness_cells": _count_by(cells, 1),
                        "empty": 0 if product else sum(1 for c in cells if c[2] is None)})
    return out


def _count_by(cells: list, index: int) -> dict[str, int]:
    out: dict[str, int] = {}
    for c in cells:
        key = f"{float(c[index]):g}"
        out[key] = out.get(key, 0) + 1
    return out


def products(path: Path) -> list[dict[str, Any]]:
    """VC品種。固定値の枠の長さを式で出すとき・枠を足すときに選ぶ(`active` のもの)。

    消すときに何が一緒に消えるかを言えるよう、**その品種を使っている枠**と
    内径の選択肢の数も添える。**有効を 0 にして隠した品種も並べる** ── 消したい
    のに一覧に出ない、を作らない。
    """
    from .. import source_db

    q = quote
    with source_db.open_source(path) as conn:
        rows = conn.execute(f"SELECT 品種名, VC厚, 業者名, 有効 FROM {q('VC品種')}"
                            " ORDER BY 表示順, rowid").fetchall()
        out = []
        for r in rows:
            blocks = [str(b[0]) for b in conn.execute(
                f"SELECT 品種名 FROM {q('早見表ブロック')} WHERE 計算品種 = ?"
                " ORDER BY 表示順, rowid", [r[0]])]
            cells = sum(int(conn.execute(
                f"SELECT COUNT(*) FROM {q('早見表値')} WHERE 品種名 = ?", [b]).fetchone()[0])
                for b in blocks)
            choices = int(conn.execute(
                f"SELECT COUNT(*) FROM {q('VC内径選択肢')} WHERE 品種名 = ?", [r[0]]).fetchone()[0])
            out.append({"name": str(r[0]), "vcatu": f"{float(r[1]):g}",
                        "vendor": str(r[2] or ""), "active": bool(r[3]),
                        "blocks": blocks, "cells": cells, "choices": choices})
    return out


def _vcatu_of(conn: sqlite3.Connection, mode: str, product: str,
              vcatu_input: str) -> tuple[float | None, str, str]:
    """長さに使う VC厚。戻りは (VC厚, どこから, 断る理由)。入れないなら VC厚 は None。"""
    if mode == LENGTH_NONE:
        return None, "", ""
    if mode == LENGTH_PRODUCT:
        found = conn.execute(f"SELECT VC厚 FROM {quote('VC品種')} WHERE 品種名 = ?",
                             [product]).fetchone()
        if found is None or not found[0]:
            return None, "", f"VC品種に「{product}」がありません。"
        return float(found[0]), f"VC品種「{product}」の VC厚 {format_vcatu(float(found[0]))}", ""
    value, problem = parse_vcatu(vcatu_input, "VC厚(mm か μm)を入れてください(または「長さは入れない」を選ぶ)。")
    if problem:
        return None, "", problem
    return value, f"入れた VC厚 {format_vcatu(value)}", ""


def parse_vcatu(text: str, empty_message: str = "VC厚(mm か μm)を入れてください。"
                ) -> tuple[float, str]:
    """直に入れた VC厚(全角も可)。**mm でも μm でも**。戻りは (VC厚 mm, 断る理由)。

    `0.10` は mm、`100` は μm として 0.10mm に直します(`VCATU_MIN_UM`)。
    単位を書き添えても読みます(`100μm` / `100um` / `0.1mm`)。
    """
    raw = unicodedata.normalize("NFKC", str(text or "")).strip()
    if not raw:
        return 0.0, empty_message
    body = raw.lower().replace(" ", "")
    unit = ""
    for suffix, name in (("μm", "um"), ("µm", "um"), ("um", "um"), ("mm", "mm")):
        if body.endswith(suffix):
            body, unit = body[: -len(suffix)], name
            break
    try:
        value = float(body)
    except ValueError:
        return 0.0, f"VC厚が数ではありません: {raw}"
    hint = "(mm なら 0.10 のように、μm なら 100 のように)"
    if value != value or value <= 0:
        return 0.0, f"VC厚は 0 より大きく入れてください{hint}: {raw}"
    if unit == "um" or (not unit and value > VCATU_MAX_MM):
        if VCATU_MIN_UM <= value <= VCATU_MAX_UM:
            return value / 1000.0, ""
        if unit == "um":
            return 0.0, (f"VC厚 {raw} は μm として {VCATU_MIN_UM:g}〜{VCATU_MAX_UM:g} の外です"
                         f"{hint}")
        if value > VCATU_MAX_UM:
            return 0.0, (f"VC厚 {raw} は μm としても厚すぎます({VCATU_MAX_UM:g}μm まで)"
                         f"{hint}")
        return 0.0, (f"VC厚 {raw} は mm なら厚すぎ({VCATU_MAX_MM:g}mm まで)、μm なら薄すぎ"
                     f"({VCATU_MIN_UM:g}μm から)です{hint}")
    if value <= VCATU_MAX_MM:
        return value, ""
    return 0.0, f"VC厚は {VCATU_MAX_MM:g}mm 以下で入れてください{hint}: {raw}"


def _rounding(conn: sqlite3.Connection) -> str:
    """早見表の丸め(アプリ設定)。**早見表に出す数と同じ丸めで入れる。**"""
    from .quick_table import ROUNDINGS
    found = conn.execute(f"SELECT 値 FROM {quote('アプリ設定')} WHERE キー = ?",
                         ["早見表の丸め"]).fetchone()
    mode = str(found[0]).strip() if found else ""
    return mode if mode in ROUNDINGS else "切り捨て"


def make_grid(path: Path, block: str, insides_text: str, thicknesses_text: str, *,
              length_mode: str = LENGTH_NONE, product: str = "", vcatu_text: str = "",
              fill_empty: bool = True, overwrite: bool = False) -> Result:
    """早見表ブロック1つに、内径 × 肉厚 のマスを**まとめて足す**。

    計算品種の無い枠は `length_mode` で長さを入れる(`LENGTH_*`。モジュールの説明)。
    """
    from .. import source_db
    from .calc import vc_length
    from .vba_compat import excel_to_int

    insides, problem = parse_numbers(insides_text, "内径", GRID_MAX_INSIDES)
    thicknesses: list[float] = []
    if not problem:
        thicknesses, problem = parse_numbers(thicknesses_text, "肉厚", GRID_MAX_THICKNESSES)
    if problem:
        return Result(False, problem, REFUSE_BAD_VALUE)
    q = quote
    try:
        with db.writing(path) as conn:
            found = conn.execute(f"SELECT 計算品種 FROM {q('早見表ブロック')} WHERE 品種名 = ?",
                                 [block]).fetchone()
            if found is None:
                return Result(False, f"早見表ブロックに「{block}」がありません。"
                                     "先に枠を足してください。", REFUSE_BAD_VALUE)
            computed = bool(found[0])
            vcatu, source, problem = (None, "", "") if computed else _vcatu_of(
                conn, length_mode if length_mode in LENGTH_MODES else LENGTH_NONE,
                product, vcatu_text)
            if problem:
                return Result(False, problem, REFUSE_BAD_VALUE)
            rounding = _rounding(conn)
            have = {(float(r[0]), float(r[1])): r[2] for r in conn.execute(
                f"SELECT 内径, 肉厚, 長さ FROM {q('早見表値')} WHERE 品種名 = ?", [block])}
            stamp = db.now_text()
            added = filled = replaced = 0
            for inside in insides:
                for thickness in thicknesses:
                    length = (None if vcatu is None else
                              excel_to_int(vc_length(thickness, vcatu, inside), rounding))
                    key = (inside, thickness)
                    if key not in have:
                        conn.execute(
                            f"INSERT INTO {q('早見表値')} (品種名, 内径, 肉厚, 長さ, 更新日時)"
                            " VALUES (?, ?, ?, ?, ?)", [block, inside, thickness, length, stamp])
                        added += 1
                        continue
                    if length is None:
                        continue
                    now = have[key]
                    if (now is None and fill_empty) or (now is not None and overwrite
                                                        and int(now) != length):
                        conn.execute(
                            f"UPDATE {q('早見表値')} SET 長さ = ?, 更新日時 = ?"
                            " WHERE 品種名 = ? AND 内径 = ? AND 肉厚 = ?",
                            [length, stamp, block, inside, thickness])
                        if now is None:
                            filled += 1
                        else:
                            replaced += 1
            skipped = len(insides) * len(thicknesses) - added
            if added or filled or replaced:
                change = {"品種名": block, "内径": [f"{v:g}" for v in insides],
                          "肉厚": [f"{v:g}" for v in thicknesses], "足したマス": added}
                if vcatu is not None:
                    change.update({"長さ": f"{source}から式で({rounding})",
                                   "長さを入れたマス": filled, "置き換えたマス": replaced})
                db.record_change(conn, "早見表値", "まとめて足す", None, None, change)
                db.bump_revision(conn)
    except db.DbLocked as exc:
        return Result(False, str(exc), REFUSE_LOCKED)
    except db.DbError as exc:
        return Result(False, str(exc), REFUSE_NO_SOURCE)
    except sqlite3.Error as exc:
        log.warning("早見表のマスを足せませんでした: %s", exc)
        return Result(False, f"マスタへ書けませんでした: {exc}", REFUSE_WRITE_FAILED)
    finally:
        # 写しの控えを捨てる。捨てないと「足したのに出ない」になる
        source_db.forget(path)
    if not (added or filled or replaced):
        return Result(True, f"「{block}」には {skipped} マスともすでにあります(書いていません)。")
    masters.invalidate()
    log.info("早見表のマスをまとめて足しました: %s 足した%d 入れた%d 置き換えた%d",
             block, added, filled, replaced)
    parts = []
    if added:
        parts.append(f"{added} マス足しました")
    if filled:
        parts.append(f"長さが空だった {filled} マスに長さを入れました")
    if replaced:
        parts.append(f"{replaced} マスの長さを置き換えました")
    head = f"「{block}」" + ("に" if added else "の") + "。".join(parts) + "。"
    if computed:
        note = "長さは計算品種の VC厚 から式で出ます。"
    elif vcatu is not None:
        note = f"長さは、{source} から式で出しました(丸め: {rounding})。"
    else:
        note = ("この枠は計算品種が空なので、長さは空欄です。VC厚 を選ぶか入れて"
                "もう一度押すと、空のマスに長さを入れます。")
    kept = (f"すでにあった {skipped} マスはそのままです。"
            if skipped and not (filled or replaced) else "")
    return Result(True, f"{head}{kept}{note}", added=added, filled=filled,
                  replaced=replaced)


# ------------------------------------------------------------------
# 品種(枠)を足す・消す・長さの出し方を変える(v3.97.0)
#
#     1行消すがないと追加したVCが永遠に消せない(早見表を先に消せ)
#     早見表値から消そうと思うと相当数消さないといけない
#     早見表に追加でVCを足すのもわかりにくい
#     固定値(式なし)に知らない間になっていたがそれもよくわからない
#
# マスタは「VC品種 ← 早見表ブロック ← 早見表値」と鎖になっていて、親を消すには
# 子を先に全部消す決まり(ON DELETE RESTRICT)。マスタ管理で1行ずつ消すと、
# 枠1つで数十行になる。ここでは**1つの取引で鎖ごと**足す・消す。
# 何が一緒に消えるかは押す前に言う(画面の確かめ)し、消した中身は変更履歴に残す。
# ------------------------------------------------------------------
#: 枠の色(早見表の紙の枠線の色)の呼び名。値は `seed.TONES`
TONE_LABELS = {"navy": "紺", "red": "赤", "blue": "青", "orange": "橙",
               "green": "緑", "teal": "青緑"}


def tones() -> list[dict[str, str]]:
    from .seed import TONES
    return [{"value": t, "label": TONE_LABELS.get(t, t)} for t in TONES]


class _Refused(Exception):
    """書く前に断る。取引は取り消す(何も書かない)。"""


def _apply(path: Path, what: str, work) -> Result:
    """`work(conn) -> Result` を**1つの取引**で流し、更新番号を上げる。"""
    from .. import source_db

    try:
        with db.writing(path) as conn:
            result = work(conn)
            db.bump_revision(conn)
    except _Refused as exc:
        return Result(False, str(exc), REFUSE_BAD_VALUE)
    except db.DbLocked as exc:
        return Result(False, str(exc), REFUSE_LOCKED)
    except db.DbError as exc:
        return Result(False, str(exc), REFUSE_NO_SOURCE)
    except sqlite3.Error as exc:
        log.warning("%sできませんでした: %s", what, exc)
        return Result(False, f"マスタへ書けませんでした: {exc}", REFUSE_WRITE_FAILED)
    finally:
        source_db.forget(path)                   # 写しの控えを捨てる(足したのに出ない、を防ぐ)
    masters.invalidate()
    log.info("%s: %s", what, result.message)
    return result


def _name(text: Any) -> str:
    """名前は**打ったまま**(前後の空白だけ取る)。NFKC は掛けない ──
    いまある名前に半角カナ(ｽﾐﾛﾝVE系)があり、変えると見つからなくなる。"""
    return str(text or "").strip()


def _next_order(conn: sqlite3.Connection, table: str) -> int:
    found = conn.execute(f"SELECT MAX(表示順) FROM {quote(table)}").fetchone()
    return int(found[0] or 0) + 1


def _cells_of(conn: sqlite3.Connection, block: str) -> list[tuple[float, float, Any]]:
    return [(float(r[0]), float(r[1]), r[2]) for r in conn.execute(
        f"SELECT 内径, 肉厚, 長さ FROM {quote('早見表値')} WHERE 品種名 = ?"
        " ORDER BY 内径, 肉厚", [block])]


def _cells_text(cells: list[tuple[float, float, Any]]) -> list[list[Any]]:
    """変更履歴に残すマス([内径, 肉厚, 長さ])。消したものを戻せるように。"""
    return [[f"{i:g}", f"{t:g}", length] for i, t, length in cells]


def add_block(path: Path, *, product: str = "", new_product: str = "", new_vcatu: str = "",
              new_vendor: str = "", block_name: str = "", formula: bool = True,
              insides_text: str = "", thicknesses_text: str = "", tone: str = "") -> Result:
    """早見表に品種(枠)を足す。**VC品種・枠・マスを1回で。**

    - `product` … いまある VC品種 を使う / `new_product` … VC品種 も新しく足す(VC厚 が要る)
    - `formula` … 長さを VC厚 から毎回式で出す(枠の計算品種に結ぶ)。False なら固定値
      ── 足すときに同じ式で出して入れ、そのあとは VC厚 を直しても変わらない
    - 枠の名前は空なら VC品種 の名前。色は空ならまだ使っていない色
    """
    from .calc import vc_length
    from .seed import TONES
    from .vba_compat import excel_to_int

    insides, problem = parse_numbers(insides_text, "内径", GRID_MAX_INSIDES)
    thicknesses: list[float] = []
    if not problem:
        thicknesses, problem = parse_numbers(thicknesses_text, "肉厚", GRID_MAX_THICKNESSES)
    new_name, picked = _name(new_product), _name(product)
    vcatu_new = 0.0
    if not problem and not (new_name or picked):
        problem = "VC品種を選ぶか、新しい VC品種の名前を入れてください。"
    if not problem and new_name:
        vcatu_new, problem = parse_vcatu(new_vcatu, "新しい VC品種の VC厚(mm か μm)を入れてください。")
    tone = _name(tone)
    if not problem and tone and tone not in TONES:
        problem = f"枠の色は {' / '.join(TONE_LABELS.get(t, t) for t in TONES)} のどれかです。"
    if problem:
        return Result(False, problem, REFUSE_BAD_VALUE)

    def work(conn: sqlite3.Connection) -> Result:
        q = quote
        stamp = db.now_text()
        if new_name:
            if conn.execute(f"SELECT 1 FROM {q('VC品種')} WHERE 品種名 = ?", [new_name]).fetchone():
                raise _Refused(f"VC品種に「{new_name}」はもうあります。"
                               "「VC品種」の一覧から選んでください。")
            name, vcatu, vendor = new_name, vcatu_new, _name(new_vendor)
            # 内径が1つなら、計算の画面で品種を選んだときに入る内径にする
            conn.execute(
                f"INSERT INTO {q('VC品種')} (表示順, 品種名, 業者名, VC厚, 内径, 有効, 備考, 更新日時)"
                " VALUES (?, ?, ?, ?, ?, 1, ?, ?)",
                [_next_order(conn, "VC品種"), name, vendor, vcatu,
                 insides[0] if len(insides) == 1 else None,
                 "VC長さ計算の設定(早見表に品種を足す)で足しました", stamp])
        else:
            found = conn.execute(f"SELECT VC厚, 業者名 FROM {q('VC品種')} WHERE 品種名 = ?",
                                 [picked]).fetchone()
            if found is None:
                raise _Refused(f"VC品種に「{picked}」がありません(消えたかもしれません)。")
            name, vcatu, vendor = picked, float(found[0]), str(found[1] or "")
        block = _name(block_name) or name
        if conn.execute(f"SELECT 1 FROM {q('早見表ブロック')} WHERE 品種名 = ?",
                        [block]).fetchone():
            raise _Refused(f"早見表に「{block}」の枠はもうあります。マスを足すなら下の"
                           "「選んだ枠を直す」で。別の枠にするなら枠の名前を変えてください。")
        used = {str(r[0]) for r in conn.execute(f"SELECT 枠色 FROM {q('早見表ブロック')}")}
        color = tone or next((t for t in TONES if t not in used), TONES[0])
        conn.execute(
            f"INSERT INTO {q('早見表ブロック')} (表示順, 品種名, 業者名, 枠色, 有効, 更新日時, 計算品種)"
            " VALUES (?, ?, ?, ?, 1, ?, ?)",
            [_next_order(conn, "早見表ブロック"), block, vendor, color, stamp,
             name if formula else None])
        rounding = _rounding(conn)
        for inside in insides:
            for thickness in thicknesses:
                length = None if formula else excel_to_int(
                    vc_length(thickness, vcatu, inside), rounding)
                conn.execute(
                    f"INSERT INTO {q('早見表値')} (品種名, 内径, 肉厚, 長さ, 更新日時)"
                    " VALUES (?, ?, ?, ?, ?)", [block, inside, thickness, length, stamp])
        count = len(insides) * len(thicknesses)
        db.record_change(conn, "早見表ブロック", "品種を足す", None, None, {
            "品種名": block, "VC品種": name, "新しいVC品種": bool(new_name),
            "VC厚": f"{vcatu:g}", "長さ": "式" if formula else f"固定値({rounding})",
            "内径": [f"{v:g}" for v in insides], "肉厚": [f"{v:g}" for v in thicknesses],
            "マス": count})
        head = f"早見表に「{block}」を足しました(マス {count})。"
        if new_name:
            head += f"VC品種「{name}」も足しました(計算の品種一覧にも出ます)。"
        if formula:
            note = f"長さは VC品種「{name}」の VC厚 {format_vcatu(vcatu)} から式で出します。"
        else:
            note = (f"長さは VC厚 {format_vcatu(vcatu)} から計算して入れました({rounding})。"
                    "固定値なので、VC厚 を直しても変わりません。")
        return Result(True, head + note, added=count, filled=0 if formula else count)

    return _apply(path, "早見表に品種を足しました", work)


def delete_block(path: Path, block: str) -> Result:
    """早見表の枠を**マスごと**消す。VC品種 は残す(計算の品種一覧に出ている)。"""
    block = _name(block)

    def work(conn: sqlite3.Connection) -> Result:
        q = quote
        found = conn.execute(f"SELECT rowid, 計算品種, 業者名, 枠色 FROM {q('早見表ブロック')}"
                             " WHERE 品種名 = ?", [block]).fetchone()
        if found is None:
            raise _Refused(f"早見表に「{block}」の枠がありません(もう消えています)。")
        cells = _cells_of(conn, block)
        conn.execute(f"DELETE FROM {q('早見表値')} WHERE 品種名 = ?", [block])
        conn.execute(f"DELETE FROM {q('早見表ブロック')} WHERE 品種名 = ?", [block])
        product = str(found[1] or "")
        db.record_change(conn, "早見表ブロック", "枠を消す", int(found[0]), {
            "品種名": block, "計算品種": product or None, "業者名": found[2],
            "枠色": found[3], "マス": _cells_text(cells)}, None)
        keep = (f"VC品種「{product}」は残しています(計算の品種一覧に出ます。"
                "要らなければ「VC品種を消す」で)。" if product else "")
        return Result(True, f"早見表から「{block}」の枠とマス {len(cells)} を消しました。{keep}")

    return _apply(path, "早見表の枠を消しました", work)


def delete_cells(path: Path, block: str, insides_text: str = "",
                 thicknesses_text: str = "") -> Result:
    """枠の中の**行(内径)・列(肉厚)**を消す。片方だけなら、その行・列のマスを全部。

    両方とも空は断る(枠ごと消すなら `delete_block`)。
    """
    block = _name(block)
    insides: list[float] = []
    thicknesses: list[float] = []
    problem = ""
    if str(insides_text or "").strip():
        insides, problem = parse_numbers(insides_text, "内径", GRID_MAX_INSIDES)
    if not problem and str(thicknesses_text or "").strip():
        thicknesses, problem = parse_numbers(thicknesses_text, "肉厚", GRID_MAX_THICKNESSES)
    if not problem and not (insides or thicknesses):
        problem = "消す内径か肉厚を入れてください(枠ごと消すなら「この枠を消す」)。"
    if problem:
        return Result(False, problem, REFUSE_BAD_VALUE)

    def hit(value: float, wanted: list[float]) -> bool:
        return not wanted or any(abs(value - w) < 1e-9 for w in wanted)

    def work(conn: sqlite3.Connection) -> Result:
        q = quote
        if not conn.execute(f"SELECT 1 FROM {q('早見表ブロック')} WHERE 品種名 = ?",
                            [block]).fetchone():
            raise _Refused(f"早見表に「{block}」の枠がありません(もう消えています)。")
        cells = _cells_of(conn, block)
        gone = [c for c in cells if hit(c[0], insides) and hit(c[1], thicknesses)]
        label = _cells_label(insides, thicknesses)
        if not gone:
            have_i = ", ".join(sorted({f"{c[0]:g}" for c in cells}, key=float)) or "なし"
            have_t = ", ".join(sorted({f"{c[1]:g}" for c in cells}, key=float)) or "なし"
            raise _Refused(f"「{block}」に {label}はありません"
                           f"(いま 内径: {have_i} / 肉厚: {have_t})。")
        for inside, thickness, _length in gone:
            conn.execute(f"DELETE FROM {q('早見表値')} WHERE 品種名 = ? AND 内径 = ? AND 肉厚 = ?",
                         [block, inside, thickness])
        db.record_change(conn, "早見表値", "マスを消す", None,
                         {"品種名": block, "マス": _cells_text(gone)}, None)
        left = len(cells) - len(gone)
        tail = (f"残りのマスは {left} です。" if left else
                "この枠のマスは無くなりました。枠ごと消すなら「この枠を消す」で。")
        return Result(True, f"「{block}」から {label}(マス {len(gone)})を消しました。{tail}")

    return _apply(path, "早見表のマスを消しました", work)


def _cells_label(insides: list[float], thicknesses: list[float]) -> str:
    i = "・".join(f"{v:g}" for v in insides)
    t = "・".join(f"{v:g}" for v in thicknesses)
    if insides and thicknesses:
        return f"内径 {i} × 肉厚 {t} のマス"
    return f"内径 {i} の行" if insides else f"肉厚 {t} の列"


def set_source(path: Path, block: str, product: str) -> Result:
    """枠の長さの出し方を変える。`product` が空なら固定値、あれば その VC品種 の式。

    **式 → 固定値は、いま早見表に出ている数のまま固定する**(切り替えた瞬間に表の
    数が変わらない)。固定値 → 式は、マスに入れてあった長さを使わなくなる
    (消さない。変更履歴にも残す)。
    """
    from .calc import vc_length
    from .vba_compat import excel_to_int

    block, product = _name(block), _name(product)

    def work(conn: sqlite3.Connection) -> Result:
        q = quote
        found = conn.execute(
            f"SELECT b.rowid, b.計算品種, p.VC厚 FROM {q('早見表ブロック')} b"
            f" LEFT JOIN {q('VC品種')} p ON p.品種名 = b.計算品種 WHERE b.品種名 = ?",
            [block]).fetchone()
        if found is None:
            raise _Refused(f"早見表に「{block}」の枠がありません(もう消えています)。")
        now = str(found[1] or "")
        cells = _cells_of(conn, block)
        if not product:
            if not now:
                raise _Refused(f"「{block}」はもう固定値です。")
            if found[2] is None:
                raise _Refused(f"計算品種「{now}」が VC品種 に無いので、いまの数が出せません。")
            vcatu, rounding, stamp = float(found[2]), _rounding(conn), db.now_text()
            for inside, thickness, _length in cells:
                conn.execute(
                    f"UPDATE {q('早見表値')} SET 長さ = ?, 更新日時 = ?"
                    " WHERE 品種名 = ? AND 内径 = ? AND 肉厚 = ?",
                    [excel_to_int(vc_length(thickness, vcatu, inside), rounding), stamp,
                     block, inside, thickness])
            conn.execute(f"UPDATE {q('早見表ブロック')} SET 計算品種 = NULL, 更新日時 = ?"
                         " WHERE 品種名 = ?", [stamp, block])
            db.record_change(conn, "早見表ブロック", "固定値にする", int(found[0]),
                             {"品種名": block, "計算品種": now, "マス": _cells_text(cells)},
                             {"品種名": block, "計算品種": None,
                              "長さ": f"VC厚 {format_vcatu(vcatu)} の式({rounding})で入れた"})
            return Result(True, f"「{block}」を固定値にしました。いま出ていた数(VC品種「{now}」の"
                                f" VC厚 {vcatu:g}mm の式)をそのまま長さとして入れました"
                                f"(マス {len(cells)})。これからは VC厚 を直しても変わりません。",
                          filled=len(cells))
        target = conn.execute(f"SELECT VC厚 FROM {q('VC品種')} WHERE 品種名 = ?",
                              [product]).fetchone()
        if target is None:
            raise _Refused(f"VC品種に「{product}」がありません。")
        if now == product:
            raise _Refused(f"「{block}」はもう VC品種「{product}」の式で出しています。")
        conn.execute(f"UPDATE {q('早見表ブロック')} SET 計算品種 = ?, 更新日時 = ?"
                     " WHERE 品種名 = ?", [product, db.now_text(), block])
        typed = [c for c in cells if c[2] is not None]
        db.record_change(conn, "早見表ブロック", "式にする", int(found[0]),
                         {"品種名": block, "計算品種": now or None,
                          "マス": _cells_text(typed) if not now else None},
                         {"品種名": block, "計算品種": product})
        unused = (f"マスに入れてあった長さ({len(typed)} マス)は使わなくなります"
                  "(変更履歴に残しています)。" if not now and typed else "")
        return Result(True, f"「{block}」の長さを VC品種「{product}」の VC厚 "
                            f"{float(target[0]):g}mm から式で出すようにしました。{unused}")

    return _apply(path, "早見表の長さの出し方を変えました", work)


def delete_product(path: Path, product: str) -> Result:
    """VC品種を消す。**その品種を使っている早見表の枠(とマス)・内径の選択肢も一緒に。**

    前は子を先に1行ずつ消す決まりで、早見表に足した品種は「永遠に消せない」
    に近かった。何が一緒に消えるかは、押す前に画面が言う(`products()` の数)。
    """
    product = _name(product)

    def work(conn: sqlite3.Connection) -> Result:
        q = quote
        found = conn.execute(f"SELECT rowid, * FROM {q('VC品種')} WHERE 品種名 = ?",
                             [product]).fetchone()
        if found is None:
            raise _Refused(f"VC品種に「{product}」がありません(もう消えています)。")
        names = [d[0] for d in conn.execute(f"SELECT * FROM {q('VC品種')} LIMIT 0").description]
        before_row = dict(zip(names, list(found)[1:]))
        blocks = [str(r[0]) for r in conn.execute(
            f"SELECT 品種名 FROM {q('早見表ブロック')} WHERE 計算品種 = ? ORDER BY 表示順, rowid",
            [product])]
        gone_blocks = {}
        for b in blocks:
            gone_blocks[b] = _cells_text(_cells_of(conn, b))
            conn.execute(f"DELETE FROM {q('早見表値')} WHERE 品種名 = ?", [b])
            conn.execute(f"DELETE FROM {q('早見表ブロック')} WHERE 品種名 = ?", [b])
        choices = [[r[0], f"{float(r[1]):g}"] for r in conn.execute(
            f"SELECT 表示名, 内径 FROM {q('VC内径選択肢')} WHERE 品種名 = ? ORDER BY 表示順",
            [product])]
        conn.execute(f"DELETE FROM {q('VC内径選択肢')} WHERE 品種名 = ?", [product])
        conn.execute(f"DELETE FROM {q('VC品種')} WHERE 品種名 = ?", [product])
        db.record_change(conn, "VC品種", "品種ごと消す", int(found[0]), {
            "VC品種": before_row, "内径選択肢": choices, "早見表の枠": gone_blocks}, None)
        parts = [f"早見表の枠「{b}」(マス {len(c)})" for b, c in gone_blocks.items()]
        if choices:
            parts.append(f"内径の選択肢 {len(choices)}")
        with_ = f"一緒に消したもの: {'・'.join(parts)}。" if parts else ""
        return Result(True, f"VC品種「{product}」を消しました(計算の品種一覧からも消えます)。{with_}")

    return _apply(path, "VC品種を消しました", work)
