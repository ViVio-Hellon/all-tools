"""いまこの端末を使っている人(ログインID)と、その端末(PC名)

python-web-tools(`packaging_tool/access_control.current_identity`)と同じ取り方:
Windows は `USERNAME` / `COMPUTERNAME` を必ず持っている。開発機(Linux)でも
動くよう、無ければ Python の一般的な手段へ落とす。試験・調査では
`ALLTOOLS_LOGIN_ID` / `ALLTOOLS_PC_NAME` で差し替えられる。
"""
from __future__ import annotations

import getpass
import os
import platform
from dataclasses import dataclass
from typing import Optional

ENV_LOGIN = "ALLTOOLS_LOGIN_ID"
ENV_HOST = "ALLTOOLS_PC_NAME"


@dataclass(frozen=True)
class Identity:
    login_id: str
    pc_name: str

    def label(self) -> str:
        return f"{self.login_id or '(不明)'} @ {self.pc_name or '(不明)'}"

    def to_dict(self) -> dict:
        return {"login_id": self.login_id, "pc_name": self.pc_name, "label": self.label()}


def _clean(value: Optional[str]) -> str:
    return (value or "").strip()


def current() -> Identity:
    login = _clean(os.environ.get(ENV_LOGIN)) or _clean(os.environ.get("USERNAME"))
    if not login:
        try:
            login = _clean(getpass.getuser())
        except Exception:                         # noqa: BLE001 - 環境依存で落ちうる
            login = ""
    host = _clean(os.environ.get(ENV_HOST)) or _clean(os.environ.get("COMPUTERNAME"))
    if not host:
        host = _clean(platform.node())
    return Identity(login_id=login, pc_name=host)
