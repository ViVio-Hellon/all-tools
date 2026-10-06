"""VC長さ計算の画面に渡す形(vc-calculator `routes/calc.py` / `quick.py` の組み立て部分)

画面が持つのは入力欄の文字だけです。品種を選ぶ・内径を選ぶ・計算するの
3つとも、**いまのマスタ**を読んでからサーバが欄を作り直して返します
(VBA `List選択` / `Big_Click` / `CommandButton1_Click` の置き換え)。
画面を開いたままほかの端末でマスタが直されても、次の操作で追いつきます。

面(`TABS`)の並びもここで決めます:

    計算          品種 → 内径 → 肉厚 → 計算。VC長さと定尺ごとの枚数、図と計算の経過
    早見表        肉厚 → 長さの表(計算と同じ式)。A4 横で印刷できる
    コイル・平板  コイル・平板の重量計算ツール(3D・グラフ・計算書の印刷)
    設定          マスタの置き場所(VC計算マスタ.sqlite3 / vc_master.sqlite3 の
                  どちらを読んでいるか)・早見表のマスをまとめて作る(管理者)
"""
from __future__ import annotations

from typing import Any

from ..vc import quick_table
from ..vc.masters import Product, Snapshot
from ..vc.vba_compat import vba_format_fixed

TABS: tuple[tuple[str, str, str], ...] = (
    ("calc", "計算", "肉厚から VC長さと、定尺ごとの枚数を出す"),
    ("quick", "早見表", "肉厚 → 長さの表(計算と同じ式)。印刷できる"),
    ("coil", "コイル・平板", "コイル・平板の重量・長さ(3D・グラフ・計算書)"),
    ("settings", "設定", "マスタの置き場所・早見表のマスをまとめて作る(管理者)"),
)
DEFAULT_TAB = "calc"

# 断りの種類。**文言から推し量らない**
REFUSE_NO_PRODUCT = "no_product"
REFUSE_NO_CHOICE = "no_choice"


def tabs() -> list[dict[str, str]]:
    return [{"key": k, "label": label, "note": note} for k, label, note in TABS]


def product_view(p: Product) -> dict[str, Any]:
    return {
        "name": p.name,
        "vendor": p.vendor,
        "vcatu": vba_format_fixed(p.vcatu, 2),
        "inside": "" if p.inside is None else vba_format_fixed(p.inside, 1),
        "chooses_inside": p.chooses_inside,
        "choices": [{"label": c.label, "inside": vba_format_fixed(c.inside, 1)}
                    for c in p.choices],
        "note": p.note,
        # 内径を選ばず、決まった内径も無い品種(マスタの入れ漏れ)
        "inside_missing": not p.chooses_inside and p.inside is None,
    }


def master_view(snap: Snapshot) -> dict[str, Any]:
    """マスタの状態。**控え・初期値で動いているときは、そう言う。**"""
    status = snap.status()
    status["usable"] = snap.usable
    status["note"] = ""
    if snap.source == "cache":
        status["note"] = (f"マスタを読めないので、最後に読めた中身({snap.read_at})で"
                          f"計算しています。理由: {snap.problem}")
    elif snap.source == "seed":
        status["note"] = ("マスタを読めないので、VBA に直書きされていた初期値で計算しています"
                          f"(マスタで直した値は効いていません)。理由: {snap.problem}")
    return status


def state(snap: Snapshot) -> dict[str, Any]:
    return {
        "products": [product_view(p) for p in snap.products],
        "sheets": [{"key": s.key, "label": s.label,
                    "length_mm": vba_format_fixed(s.length_mm, 0)} for s in snap.sheets],
        "reverse": snap.reverse_enabled(),
        "master": master_view(snap),
    }


def quick(snap: Snapshot) -> dict[str, Any]:
    return {"quick": quick_table.build(snap), "master": master_view(snap)}


# ------------------------------------------------------------------
# 設定 ── マスタの置き場所と、2つの名前の関係
# ------------------------------------------------------------------
#: 探す名前ごとの説明(優先の順)。**画面にそのまま出る字**
NAME_NOTES: tuple[str, ...] = (
    "このツールの名前。あれば、こちらを使います",
    "vc-calculator が作る名前。上が無いときに使います",
)


def place_view() -> dict[str, Any]:
    """VC計算マスタの置き場所と、**どのファイルを読んでいるか。**

        VC計算マスタ.sqlite3 と vc_master.sqlite3 の関係も表示しておいて
        ください(どちらが優先)

    同じフォルダで2つの名前を探します(`config.SETTINGS.vc_master_path`):
    ① `VC計算マスタ.sqlite3` があればそれ、② 無ければ `vc_master.sqlite3`、
    どちらも無ければ ① を初めて使うときに作ります。**2つともあると ②は
    読まれません** ── vc-calculator が ② を直しても、こちらには効きません。
    """
    from .. import config, user_settings
    from ..config import SETTINGS

    folder = SETTINGS.vc_master_dir
    used = SETTINGS.vc_master_path
    stored = user_settings.load_all().get(config.KEY_VC_MASTER_DIR)
    text = stored.strip() if isinstance(stored, str) else ""
    try:
        folder_ok = folder.is_dir()
    except OSError:                                   # 共有に届かない
        folder_ok = False

    names = []
    for rank, path in enumerate(SETTINGS.vc_master_candidates(), start=1):
        try:
            exists = path.is_file()
        except OSError:
            exists = False
        names.append({
            "rank": rank, "name": path.name, "path": str(path),
            "exists": exists, "used": exists and path == used,
            "note": NAME_NOTES[rank - 1] if rank <= len(NAME_NOTES) else "",
        })
    found = [n for n in names if n["exists"]]
    # 2つともある: 下の名前は読まれていない(直しても効かない)
    ignored = [n for n in found if not n["used"]]
    if not folder_ok:
        status, level = (f"このフォルダがありません({folder})。マスタを作れないので、"
                         "最後に読めた中身か VBA の初期値で計算しています"), "error"
    elif not found:
        status, level = (f"まだどちらもありません。VC長さ計算を開いたときに "
                         f"{names[0]['name']} を VBA の初期値で作ります"), "info"
    elif ignored:
        status, level = (f"{used.name} を読んでいます。{ignored[0]['name']} も"
                         "ありますが、読んでいません ── vc-calculator がそちらを"
                         "直しても、こちらには効きません。どちらか1つに寄せてください"), "warn"
    else:
        status, level = f"{used.name} を読んでいます", "ok"
    return {
        "key": config.KEY_VC_MASTER_DIR,
        "label": "VC計算マスタの置き場所",
        "value": text,
        "is_default": not text,
        "dir": str(folder),
        "reference_dir": str(SETTINGS.gw_reference_dir),
        "dir_exists": folder_ok,
        "names": names,
        "used": str(used),
        "used_name": used.name,
        "status": status,
        "level": level,
        "settings_url": "/settings?tab=paths",
    }
