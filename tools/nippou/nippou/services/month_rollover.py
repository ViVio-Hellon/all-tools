"""月替わりの書き出し (VBA ``月替わりチェック実行`` → ``シートコピー保存処理``)

【VBA での順】
印刷保存の最後、**手動で押したときだけ**走っていました。

    1. 月替わりか?       … 溜まっているシートの日付で決める
    2. True なら書き出す … その月ぶんを新しいブックにして共有へ保存
    3. そのあと消す      … ブックからその月の**集計シート**を消す

自動印刷のときは丸ごと飛ばします(`isAutoMode` が True の側)。**誰も
見ていないところで月を片付けない**、という判断です。ここも同じにします。

【3番目(`月初削除`)は移植しません】
VBA が消していたのは **Excelシート(見た目の写し)**で、元データは
Access(`日報データ`)に残っていました。シートはいつでも作り直せるので
消してよかったのです。

このツールの日報は**値**です。(年月日, ライン, 直) で呼び出せて、
そこから紙もグラフも何度でも作れる ── **消したら二度と出せません。**
なので月替わりにやるのは**書き出しだけ**で、手元のデータはそのまま
残します。

    月替わり = 1か月ぶんを月別フォルダへ**写す**(ツール無しで読める形に)
             ≠ 手元から消す
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any, Callable, Optional

from ..db.repository import NippouRepository
from ..logging_setup import get_logger
from ..logic.month_roll import RollDecision, decide
from ..reporting import month_export
from ..reporting.month_export import MonthExport

log = get_logger("services.month_rollover")


@dataclass
class RolloverResult:
    """月替わりの結果。**やったことを全部持つ。**"""

    decision: RollDecision
    exports: list[MonthExport] = field(default_factory=list)
    failed: bool = False
    pending: bool = False          # まだ共有へ送っていないページが残っている

    @property
    def ran(self) -> bool:
        return bool(self.exports)

    @property
    def message(self) -> str:
        if not self.decision.due:
            return f"月替わりではありません（{self.decision.reason}）"
        if not self.exports:
            return f"{self.decision.label}: 書き出すデータがありませんでした"
        where = self.exports[0].folder.parent
        head = (f"{self.decision.label} を書き出しました"
                f"（{len(self.exports)}ライン / {sum(e.rows for e in self.exports)}行）"
                f"\n{where}")
        if self.failed:
            return (f"{head}\n**書き出しが途中で終わりました。**"
                    "書き出し先に届いているか確かめてください")
        # **消さない。** 日報は値で残すもので、ここで消したら二度と
        # 同じ紙を出せない(VBA が消していたのはシートの写しだった)
        tail = "手元のデータはそのまま残ります(消しません)"
        if self.pending:
            tail += "。共有へ送っていないページが残っています"
        return f"{head}\n{tail}"

    def as_dict(self) -> dict:
        return {"decision": self.decision.as_dict(), "ran": self.ran,
                "exports": [e.as_dict() for e in self.exports],
                "failed": self.failed, "pending": self.pending,
                "message": self.message}


def check(repo: NippouRepository, today: Optional[date] = None) -> RollDecision:
    """月替わりか(port of ``月替わりチェック実行``)。**消しも書きもしない。**

    書き出しを済ませた月は外します(`repository.settled_months`)。
    """
    return decide(repo.month_keys(), today or date.today(),
                  settled=repo.settled_months())


#: 共有へ保存1回で片付ける月の上限。取り込みで何年ぶんも入ったとき、
#: 1回の保存が長く止まらないように(残りは次の保存で片付く)
MAX_MONTHS_PER_RUN = 12


def settle(repo: NippouRepository, result: "RolloverResult",
           second_ok: bool = True) -> bool:
    """書き出しを**済ませた**として覚える。覚えたら True。

    済んだと言えるのは、次が全部そろったときだけです:

        1つ目に書けた                     (`failed` でない)
        その月に未送信のページが無い      (`pending` でない)
        2つ目(決めてあれば)にも出せた     (`second_ok`)

    どれかが欠けたら覚えません ── 次の「共有へ保存」でもう1度書き出します
    (同じ中身を上書きするだけなので害はありません)。
    """
    if not result.ran or result.failed or result.pending or not second_ok:
        return False
    repo.mark_rolled(result.decision.year, result.decision.month)
    return True


def catch_up(repo: NippouRepository, base_dir: Path, *,
             second: Optional[Callable[["RolloverResult"], Any]] = None,
             today: Optional[date] = None) -> list[tuple["RolloverResult", Any]]:
    """**済んでいない月を、古い順にぜんぶ**書き出す(共有へ保存の直後・押したとき)。

    1か月ずつ: 1つ目へ書く → `second` で2つ目へ出す → 済んだと覚える。
    済ませられなかった月(未送信が残る・書けなかった)があれば、そこで
    止めます ── 次の「共有へ保存」でその月からやり直します。

    `second` の戻りは `ok` を持つもの(`second_output.Outcome`)。
    返すのは (1つ目の結果, 2つ目の結果) の並び。
    """
    done: list[tuple[RolloverResult, Any]] = []
    for _ in range(MAX_MONTHS_PER_RUN):
        decision = check(repo, today)
        if not decision.due:
            break
        result = run(repo, base_dir, decision=decision)
        extra = second(result) if second is not None else None
        done.append((result, extra))
        if not settle(repo, result, second_ok=getattr(extra, "ok", True)):
            break
    return done


def stop_labels() -> dict[str, str]:
    """停止の記号 → 内訳名。停止集計CSVに名前を載せるために引く。

    読めなければ空 ── 記号だけで書き出します(名前が無くても、
    どの記号が何分だったかは残ります)。
    """
    from ..access_bridge import stop_master

    try:
        by_category = stop_master.load_all_categories()
    except Exception:                             # noqa: BLE001 - 書き出しを止めない
        log.warning("停止内訳マスタを読めませんでした。記号だけで書き出します")
        return {}
    return {r.code: r.label
            for items in by_category.values() for r in items if r.code}


def run(repo: NippouRepository, base_dir: Path, *,
        today: Optional[date] = None,
        decision: Optional[RollDecision] = None,
        month: Optional[tuple[int, int]] = None) -> RolloverResult:
    """月替わりなら、1か月ぶんを月別フォルダへ書き出す。**消しません。**

    `month` に (年, 月) を渡すと、月替わりの判定を飛ばしてその月を
    書き出します ── 過ぎた月の写しを取り直したいときの入口です。
    """
    if month is not None:
        found = RollDecision(True, month[0], month[1],
                             reason=f"{month[0]}年{month[1]}月を指定")
    else:
        found = decision or check(repo, today)
    if not found.due:
        return RolloverResult(found)

    records = repo.month_headers(found.year, found.month)
    if not records:
        return RolloverResult(found)

    # **どのラインが動いたか**は日報のほうで数えます ── 集計はライン
    # ごとに読み直すので、先にラインの一覧が要ります
    lines = sorted({header.line for header, _ in records})
    exports: list[MonthExport] = []
    for line in lines:
        exports.append(month_export.write_month(
            repo, year=found.year, month=found.month, line=line,
            base_dir=base_dir))

    # **書けたことを確かめる。** 共有が落ちていると、`mkdir`/`open` が
    # 通っても中身が無いことがある
    written = [p for e in exports for p in e.files]
    if not all(p.exists() and p.stat().st_size > 0 for p in written):
        log.error("月替わりの書き出しが途中で終わりました: %s", found.label)
        return RolloverResult(found, exports, failed=True)

    pending = repo.month_has_pending(found.year, found.month)
    log.info("月替わり: %s を書き出しました(%dライン)。手元はそのまま",
             found.label, len(exports))
    return RolloverResult(found, exports, pending=pending)
