"""マスタ管理のビューモデル

``master_admin`` が持つ「直せるか・何が入っているか」を、画面がそのまま
描ける形へ並べ替えるだけ。判断はしない。

**直せないときも中身は出す。** 見られないと、参照パスが合っているのか
どうかすら確かめられない ── 「読めません」しか出ないのが一番困る。
"""

from __future__ import annotations

from typing import Any

from .. import master_admin


def page_dict(view: master_admin.Page) -> dict[str, Any]:
    return {
        "table": view.table,
        "label": view.label,
        "editable": view.editable,
        # 直せなくても**消せる**表がある(端末一覧)。画面はこれを見て
        # 削除のボタンだけ出す
        "removable": master_admin.is_removable(view.table),
        "reason": view.reason,
        "why": view.why,
        "source": view.source,
        # 鍵の列は**表ごとに違う**(班員名簿は管理番号、端末一覧は端末キー)
        "row_key": master_admin.row_key(view.table),
        "columns": [
            # **列ごとに、直せる/打てるが違う表がある**(端末一覧)。
            # 画面はこれを見て、打てる欄と読むだけの欄を描き分ける
            {"name": c.name, "required": c.required, "is_key": c.is_key,
             "editable": c.editable, "at_create": c.at_create,
             "placeholder": c.placeholder,
             # 入力候補。**候補以外も打てる**(共用の表で、別ツールの値も入る)
             "suggestions": list(c.suggestions)}
            for c in view.columns
        ],
        "rows": view.rows,
        "total": view.total,
        "truncated": view.truncated,
        "limit": master_admin.ROW_LIMIT,
        "query": view.query,
        # 並べ替えている列と向き(空なら表の既定)。画面は印を付けるだけ
        "sort": view.sort,
        "desc": view.desc,
        # 表の選択肢。**見るだけの表(端末一覧)もここに並ぶ** ──
        # 確かめたいものと直したいものを別の画面に分けると、探すところから
        # 始まる。画面は ``editable`` を見て描き分けるだけでよい
        "tables": [{"table": m.table, "label": m.label,
                    "editable": m.editable}
                   for m in master_admin.MANAGED],
    }


def result_dict(result: master_admin.Result) -> dict[str, Any]:
    return {"ok": result.ok, "message": result.message, "reason": result.reason}
