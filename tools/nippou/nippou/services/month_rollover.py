"""月替わりの書き出し (VBA ``月替わりチェック実行`` → ``シートコピー保存処理``)

【VBA での順】
印刷保存の最後、**手動で押したときだけ**走っていました。

    1. 月替わりか?       … 溜まっているシートの日付で決める
    2. True なら書き出す … その月ぶんを新しいブックにして共有へ保存
    3. そのあと消す      … ブックからその月の**集計シート**を消す

自動印刷のときは丸ごと飛ばします(`isAutoMode` が True の側)。**誰も
見ていないところで月を片付けない**、という判断でした。

**ここは v4.39.2 から自動確定でも書き出します**(利用者の指示「自動確定も月替わりの書き出しを
行う」)。手で押す「共有へ保存」と自動確定を1本の流れにしたため(`services/push_flow`)。
書き出すだけで手元は消さず(下)、その月に未送信の直が残っていれば見送るので、誰も見ていない
ところで走っても失うものはありません。

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
    #: **書き出さずに見送った**(その月にまだ共有へ送っていない直がある)。
    #: 共有へ保存の直後だけ(`catch_up(hold_pending=True)`。v4.35.0)
    held: bool = False
    #: **この端末では書き出さなかったライン** → 共有にあって手元に無いページの数
    #: (この端末のラインではなく、手元にはその月の一部しか無い ── `_partial_lines`)
    skipped: dict[str, int] = field(default_factory=dict)

    @property
    def ran(self) -> bool:
        return bool(self.exports)

    @property
    def message(self) -> str:
        if not self.decision.due:
            return f"月替わりではありません（{self.decision.reason}）"
        if self.held:
            return (f"{self.decision.label} の書き出しは見送りました ── その月の直に、"
                    "まだ共有へ送っていないものがあります"
                    "(送り終えたあとの共有へ保存で書き出します)")
        if not self.exports:
            if self.skipped:
                return f"{self.decision.label}: {self._skipped_text()}"
            return f"{self.decision.label}: 書き出すデータがありませんでした"
        where = self.exports[0].folder.parent
        head = (f"{self.decision.label} を書き出しました"
                f"（{len(self.exports)}ライン / {sum(e.rows for e in self.exports)}行）"
                f"\n{where}")
        if self.skipped:
            head += f"\n{self._skipped_text()}"
        if self.failed:
            return (f"{head}\n**書き出しが途中で終わりました。**"
                    "書き出し先に届いているか確かめてください")
        # **消さない。** 日報は値で残すもので、ここで消したら二度と
        # 同じ紙を出せない(VBA が消していたのはシートの写しだった)
        tail = "手元のデータはそのまま残ります(消しません)"
        if self.pending:
            tail += "。共有へ送っていないページが残っています"
        return f"{head}\n{tail}"

    def _skipped_text(self) -> str:
        from ..logic import line_names

        names = "・".join(line_names.label(ln) for ln in self.skipped)
        missing = sum(self.skipped.values())
        return (f"{names} はこの端末では書き出しません ── この端末にはその月の一部しか無く"
                f"(共有にある{missing}ページが手元にありません)、書き出すと共有の月の書き出しから"
                "そのぶんが消えるためです。書き出しはそのラインの端末が行います")

    def as_dict(self) -> dict:
        return {"decision": self.decision.as_dict(), "ran": self.ran,
                "exports": [e.as_dict() for e in self.exports],
                "failed": self.failed, "pending": self.pending,
                "held": self.held, "skipped": dict(self.skipped),
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
             today: Optional[date] = None,
             hold_pending: bool = False) -> list[tuple["RolloverResult", Any]]:
    """**済んでいない月を、古い順にぜんぶ**書き出す(共有へ保存の直後・押したとき)。

    1か月ずつ: 1つ目へ書く → `second` で2つ目へ出す → 済んだと覚える。
    済ませられなかった月(未送信が残る・書けなかった)があれば、そこで
    止めます ── 次の「共有へ保存」でその月からやり直します。

    `hold_pending` は共有へ保存の直後から(v4.35.0)。**その月にまだ共有へ
    送っていない直があれば、書き出さずに見送ります**(`held` の結果を1つ返して
    止める)。共有へ保存を「いまの直」と「過去分」に分けたので、いまの直だけ
    送れて前の月の直が残っている、が起きます ── 送っていない直を抜いたまま
    月を書き出さない(前は保存ぜんぶが止まっていたので、ここまで来ませんでした)。
    押して走らせたとき(`hold_pending` なし)は今までどおり書き出して、
    未送信が残ることを知らせます。

    `second` の戻りは `ok` を持つもの(`second_output.Outcome`)。
    返すのは (1つ目の結果, 2つ目の結果) の並び。
    """
    done: list[tuple[RolloverResult, Any]] = []
    for _ in range(MAX_MONTHS_PER_RUN):
        decision = check(repo, today)
        if not decision.due:
            break
        if hold_pending and repo.month_has_pending(decision.year, decision.month):
            log.info("月替わり: %s に未送信の直があるので、書き出しを見送ります",
                     decision.label)
            done.append((RolloverResult(decision, held=True), None))
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
    skipped = _partial_lines(records, found.year, found.month, lines)
    lines = [ln for ln in lines if ln not in skipped]
    if not lines:
        return RolloverResult(found, skipped=skipped)
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
        return RolloverResult(found, exports, failed=True, skipped=skipped)

    pending = repo.month_has_pending(found.year, found.month)
    log.info("月替わり: %s を書き出しました(%dライン)。手元はそのまま",
             found.label, len(exports))
    return RolloverResult(found, exports, pending=pending, skipped=skipped)


def _partial_lines(records, year: int, month: int, lines: list[str]) -> dict[str, int]:
    """**この端末で書き出してはいけないライン** → 共有にあって手元に無いページの数。

        管理者のPCで、ほかのライン(HVC)の9月30日 2直を控えから呼び出して直し、共有へ保存
          → 管理者のPCの手元にある9月の HVC は、その1直だけ
          → 月替わりの書き出しが、共有の 自動集計\\HVC\\2026.09 を**その1直だけの中身で**
            上書きしていた(HVC の端末が書き出した9月のほかの直が、黙って消える)

    月の書き出しは手元の日報から作るので、**その月のそのラインを全部持っている端末**が
    書くものです。そこで、**この端末のラインではない**ラインのうち、共有の日報データに
    あるのに手元に無いページがあるものは書き出しません(知らせで言います)。

    この端末のラインは今までどおり書き出します(1ライン=1端末)。共有を確かめられない
    とき(Access・届かない)も今までどおりです ── 手元に全部ある(過去の日報を取り込んだ)
    ほかのラインも、今までどおり書き出します。
    """
    from .. import work_context

    own = (work_context.stored_terminal_line()
           if work_context.terminal_line_decided() else "")
    mine: dict[str, set[tuple[str, str, int]]] = {}
    for header, _ in records:
        mine.setdefault(header.line, set()).add(
            (header.report_date, header.shift, int(header.page)))
    out: dict[str, int] = {}
    for line in lines:
        if line == own:
            continue
        shared = _shared_month_pages(line, year, month)
        if shared is None:
            continue
        missing = len(shared - mine.get(line, set()))
        if missing:
            log.warning("月替わり: %s の %d年%d月 はこの端末に一部しか無いので書き出しません"
                        "(共有にあって手元に無いページ %d)", line, year, month, missing)
            out[line] = missing
    return out


def _shared_month_pages(line: str, year: int, month: int
                        ) -> Optional[set[tuple[str, str, int]]]:
    """共有の日報データにある、そのライン・その月の (報告日, 直, ページ)。**確かめられなければ None。**"""
    import sqlite3

    from ..access_bridge import pusher, sqlite_backend
    from ..config import SETTINGS
    from ..logic.shift import parse_business_date

    path = SETTINGS.access_db_path
    try:
        if not pusher.is_sqlite_target(path) or not path.is_file():
            return None
        table = pusher.header_table_name(line)
        conn = sqlite_backend.connect(path)
        try:
            if not conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
                                (table,)).fetchone():
                return set()
            page = sqlite_backend.page_column(conn, table)
            rows = conn.execute(
                f'SELECT "報告日", "直", {page.sql()} FROM {sqlite_backend.quote(table)}'
                ' WHERE "報告日" LIKE ?', (f"{year}年{month}月%",)).fetchall()
        finally:
            conn.close()
    except (OSError, sqlite3.Error) as exc:
        log.warning("月替わり: 共有の日報データを確かめられませんでした: %s", exc)
        return None
    found: set[tuple[str, str, int]] = set()
    for report_date, shift, page_no in rows:
        day = parse_business_date(str(report_date or ""))
        if day is None or (day.year, day.month) != (year, month):
            continue                              # 「2026年1月%」に 10〜12月も当たる
        try:
            found.add((str(report_date), str(shift), int(page_no)))
        except (TypeError, ValueError):
            continue
    return found
