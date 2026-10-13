"""配布設定を書き出すだけの入口 ── 日報複合ツールの大設定が子として起こす

    python export_distribution.py

大設定の「4ツールの分もまとめて書き出す」が、このフォルダを作業フォルダにして起こします
(各ツールは別のプロセス・別の手元の領域で動き、パッケージの名前もぶつかるので、
入口のプロセスからは直接読み込まない)。

書き出すのは、設定画面の「配布設定を書き出す」を**画面の既定の選び方のまま**押したのと同じもの
(既定でチェックの入る項目 ── 表示(ダーク/ライト)は外す。画面はこの端末で変えていない項目を
選べなくしているが、そういう項目は書き出しても入らないので同じ)。
結果は標準出力に JSON 1行で返します:

    {"ok": true, "message": "...", "reason": "", "applied": [...], "path": "..."}

書き出せる設定が無い(この端末で変えていない = 既定のまま)ときは ok=false・reason="nothing"。
大設定はそれを失敗とは分けて「既定のままなので要りません」と出します。

業務の入れ物(`app/business.py`)は作らない(Excel の見張りなどが要らない)。
設定と配布設定だけを組み立てる。

【パスワードを聞かない】確かめるのは呼ぶ側(大設定の鍵 = 管理者パスワード)。この入口を
直接起こせる人は、ツールのフォルダも端末の設定ファイルも直接書き換えられる人なので、
ここでパスワードを省いても権限の抜け道にはならない(`_Checked`)。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any, Dict

APP_ROOT = Path(__file__).resolve().parent
if str(APP_ROOT) not in sys.path:
    sys.path.insert(0, str(APP_ROOT))


class _Checked:
    """管理者パスワードは呼ぶ側(大設定)が確かめた。`Distribution` にはそう答える。"""

    def verify(self, password: str) -> bool:  # noqa: ARG002 - 確かめ済み
        return True


def run() -> Dict[str, Any]:
    from app.repositories.settings_repository import SettingsRepository
    from app.services.distribution import ITEMS, Distribution
    from app.services.settings_service import SettingsService, load_inspection_config
    from core import app_config
    from core.logging_utils import get_logger

    settings = SettingsService(
        load_inspection_config(),
        SettingsRepository(str(app_config.local_dir("data")), str(app_config.local_dir("backup"))),
        get_logger("app.settings"))
    distribution = Distribution(settings, _Checked(), get_logger("app.distribution"))
    items = [key for key, _, default, _ in ITEMS if default]
    result = distribution.export("", items)
    return {"ok": result.ok, "message": result.message, "reason": result.reason,
            "applied": result.applied, "path": str(distribution.dir)}


def main() -> int:
    # 標準出力は答えの1行だけ。ツールの中の print などが混ざらないよう、本物は取っておき
    # ほかは標準エラーへ回す
    out = sys.stdout
    sys.stdout = sys.stderr
    try:
        reply = run()
    except Exception as exc:                      # noqa: BLE001 - 理由を答えに入れて返す
        reply = {"ok": False, "message": f"配布設定を書き出せませんでした: {exc}", "reason": "failed"}
    out.buffer.write((json.dumps(reply, ensure_ascii=False) + "\n").encode("utf-8"))
    out.flush()
    return 0 if reply["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
