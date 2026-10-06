"""業務として断るときの例外と、応答の形。

応答の形は python-web-tools と同じ(設計書 §4):
    HTTPステータス + {"error": {"code": "...", "message": "現場向けの日本語"}}

HTTPステータスで断りの種類を分ける。
    400 入力の形が違う(サーバの状態は動いていない)
    422 形は正しいが業務として断る(例: 選んでいない・ファイルが無い)
    409 別の処理が先に動いている(例: 印刷中・一覧が古い)

**文言はサーバが持つ。** 画面ごとに言い回しが割れない。
"""
from __future__ import annotations

from typing import Any, Dict, Optional


class ApiError(Exception):
    """`context` は画面には出さず、出来事の記録(なぜなぜ分析)にだけ残すもの
    (対象の点検表・ファイル・プリンターなど)。"""

    def __init__(self, status: int, code: str, message: str, detail: Optional[str] = None,
                 context: Optional[Dict[str, Any]] = None):
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message
        self.detail = detail
        self.context: Dict[str, Any] = dict(context or {})

    def body(self, ref: str = "") -> Dict[str, Any]:
        error: Dict[str, Any] = {"code": self.code, "message": self.message}
        if self.detail:
            error["detail"] = self.detail
        if ref:
            # 問い合わせ番号。画面の断りの文に添えて出す(ログから引ける)
            error["ref"] = ref
        return {"error": error}


def error_body(code: str, message: str, detail: Optional[str] = None, ref: str = "") -> Dict[str, Any]:
    return ApiError(0, code, message, detail).body(ref)
