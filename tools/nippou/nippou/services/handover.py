"""直の引き継ぎ ── **事実を集めて、`logic/handover` に判断させる**

判断そのものは `logic/handover.py` が持ちます(純ロジック)。ここは
DBを読んで、あちらが要る事実だけを渡します:

    ・共有へ未送信の直は何か(`pending_sync_headers`)
    ・その直に直すところがあるか(`shift_check.run` = 7項目ぜんぶ)
    ・もう引き継がれているか(`shift_handover`)
    ・**いまの直**にもう保存があるか(打ち始めていれば止めない)

【「前の直」ではなく「いまの直ではない未送信ぜんぶ」を見ます】
止めたいのは「前の直の忘れ物」ですが、**忘れ物は1直ぶんとは限りません。**
3直が忘れ、1直も忘れ、2直が来た ── という積み方が実際に起きています。
1つ前だけを見ると、その手前が永久に隠れます。

いっぽうで**いまの直だけは外します。** 打っている最中の直を「共有へ
出ていません」と言うのは筋が違います(まだ終わっていないので)。
"""
from __future__ import annotations

from typing import Optional

from ..db.repository import NippouRepository
from ..logging_setup import get_logger
from ..logic import handover as logic
from ..logic.handover import Gate, Left

log = get_logger("services.handover")


def _problems(report) -> list[str]:
    """チェックの結果を、画面に出す1行ずつの文にする。"""
    return [f"{f.where} {f.message}".strip() for f in report.findings]


def left_behind(repo: NippouRepository, *, current_key: tuple[str, str, str],
                admin: bool = False) -> list[Left]:
    """**いまの直ではない**、共有へ未送信の直。新しい順ではなく直順。

    直すところがあるかは `shift_check.run`(7項目ぜんぶ)で見ます ──
    共有へ出すときに通る関門と**同じもの**でなければ、「押せば済む」と
    言っておいて押したら断られる、が起きます。
    """
    from . import shift_check

    keys: list[tuple[str, str, str]] = []
    for header in repo.pending_sync_headers():
        key = (header.report_date, header.line, header.shift)
        if key == current_key or key in keys:
            continue
        keys.append(key)
    if not keys:
        return []

    # マスタは直の数だけ読み直さない(共有へ何度も取りに行くことになる)
    codes = list(shift_check.known_stop_codes())
    found: list[Left] = []
    for key in keys:
        report = shift_check.run(repo, *key, admin=admin, codes=codes)
        taken = repo.handover_of(*key) or {}
        found.append(Left(
            report_date=key[0], line=key[1], shift=key[2],
            pages=report.pages, problems=_problems(report),
            taken_by=taken.get("taken_by", ""),
            taken_at=taken.get("taken_at", "")))
    return found


def gate(repo: NippouRepository, *, current_key: tuple[str, str, str],
         admin: bool = False, read_only: bool = False) -> Gate:
    """いまの画面に関門を立てるか。**判断は `logic/handover.decide`。**

    `started` は「いまの直にもう保存があるか」です ── 打ち始めた人を
    途中で止めないための唯一の条件で、ここで数えます。
    """
    left = left_behind(repo, current_key=current_key, admin=admin)
    if not left:
        return Gate()
    started = bool(repo.saved_pages(*current_key))
    return logic.decide(left, started=started, read_only=read_only)


def take_over(repo: NippouRepository, *, current_key: tuple[str, str, str],
              worker: str, admin: bool = False) -> Gate:
    """残っている直を**引き継いだことにする**。断りは片付きません。

    引き継ぐのは「直せないまま次へ進む」宣言です。記録するのは
    **誰が・いつ・何を残したまま**の3つで、直ったことにはしません
    (`daily_header.dirty` は立ったままなので、数にも帯にも出続けます)。
    """
    left = left_behind(repo, current_key=current_key, admin=admin)
    for item in left:
        if item.taken_by == worker and item.taken:
            continue                  # 押し直し。同じ人なら書き換えない
        try:
            repo.record_handover(
                item.report_date, item.line, item.shift,
                taken_by=worker, findings=logic.summarize(item.problems))
        except Exception:             # noqa: BLE001 - 入力は始めさせる
            # **書けなくても通します。** 記録は大事ですが、記録できない
            # ことを理由にラインを止めるほうが重い
            log.exception("引き継ぎを残せませんでした key=%s", item.key_text)
    log.info("引き継ぎました: %s ← %s",
             "、".join(i.key_text for i in left), worker or "(名前なし)")
    return gate(repo, current_key=current_key, admin=admin)


def notes(left) -> list[str]:
    """引き継ぎ済みのものを1行ずつ。**共有へ出るまで出し続ける帯。**"""
    found = [logic.taken_note(item) for item in left]
    return [text for text in found if text]


def refusal(gate_result: Optional[Gate]) -> str:
    """止めるなら断りの文、通してよければ空。**保存の関門が使います。**"""
    if gate_result is None or not gate_result.blocked:
        return ""
    return gate_result.message
