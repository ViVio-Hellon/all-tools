"""直の紙(印刷用HTML)を出力先へ残す ── **残す道は1本** (v4.39.2)

残すのは次の4か所で、どれもここを通します:

    日報入力の「印刷」            `app/routes/printing.report`(`save=1`)
    共有へ保存(手で押す)        `services/push_flow.after_push`
    自動確定・次の直の拾い        `services/push_flow.after_push`(v4.39.2 から。前は残していなかった)
    当直をDBから復旧(管理者)    `app/routes/settings.restore_shift`

前は画面の道(`app/routes/printing.save_paper`)の中にあり、自動確定(サービス)からは呼べません
でした ── そのため自動で共有へ送った直には紙が残らず、手で押したときと食い違っていました。

置き場所は「集計CSV・印刷用HTMLの出力パス」の
`<ライン>\\印刷\\<yyyy.mm>\\<dd>\\印刷_2026年8月3日_L-1_1直_1.html`
(`print_format.default_output_path`)。同じ直・同じページは**上書き**します(残るのは最新の紙)。

**書けなくても止めません。** 返すのは `{"files": [...], "errors": [...], "dir": "..."}`。呼んだ側が
一言添えるだけで、印刷も共有への保存もそのまま進みます。
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Optional

from ..config import SETTINGS
from ..logging_setup import get_logger
from ..reporting import print_format

log = get_logger("services.paper_files")


def aggregate_of(repo: Any, report_date: str, line: str, shift: str) -> dict:
    """紙の頭に載せる、その直の集計。**刷れないよりは集計なしで刷る。**

    残してある値を読むだけですが、まだ集計の無い直(この仕組みより前に保存されたぶん)では
    その場で作ります。そこで何かあっても、紙を出すのを止める理由にはなりません。
    """
    from . import summary as summary_service

    try:
        found = summary_service.for_date(repo, report_date, line)
        record = next((r for r in found if r.shift == shift), None)
        if record is None:
            return {}
        rows = summary_service.shift_lot_rows(repo, record)
        return {"summary": record, "stops": summary_service.stop_items(rows)}
    except Exception:                     # noqa: BLE001 - 紙は出す
        log.exception("印刷に載せる集計の取得に失敗しました %s/%s/%s", report_date, line, shift)
        return {}


def backfill_stamps(repo: Any, report_date: str, line: str, shift: str) -> dict[int, str]:
    """その直で後から作ったページの印(ページ → 字)。**紙にも残す**(v4.0.0)。"""
    from ..logic import backfill as backfill_rule

    try:
        found = repo.backfills(report_date, line, shift)
    except Exception:                             # noqa: BLE001 - 印が出ないだけ
        log.exception("後日作成の印を読めませんでした")
        return {}
    return {key[3]: backfill_rule.stamp_text(info) for key, info in found.items()}


def local_pages(repo: Any, report_date: str, line: str, shift: str) -> list:
    """手元の作業用DBにあるその直の全ページ。"""
    numbers = repo.saved_pages(report_date, line, shift)
    return [p for p in (repo.load(report_date, line, shift, n) for n in numbers) if p]


def save_shift(repo: Any, report_date: str, line: str, shift: str, pages: Optional[list] = None,
               *, generated_at: str = "") -> dict:
    """その直の紙を**ページごとに1ファイル**で残す。`pages` を省くと手元の全ページ。"""
    out: dict = {"files": [], "errors": [], "dir": ""}
    try:
        if pages is None:
            pages = local_pages(repo, report_date, line, shift)
        if not pages:
            return out
        aggregate = aggregate_of(repo, report_date, line, shift)
        stamps = backfill_stamps(repo, report_date, line, shift)
        when = generated_at or datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        for header, details in pages:
            try:
                path = print_format.default_output_path(SETTINGS.report_output_dir, header)
                print_format.write_print_html(header, details, path, generated_at=when,
                                              stamp=stamps.get(int(header.page), ""), **aggregate)
                out["files"].append(str(path))
                out["dir"] = str(path.parent)
            except Exception as exc:              # noqa: BLE001 - 次のページへ
                log.exception("印刷用HTMLを残せませんでした %s/%s/%s/%s",
                              report_date, line, shift, header.page)
                out["errors"].append(f"{header.page}ページ: {exc}")
    except Exception as exc:                      # noqa: BLE001 - 印刷・保存は止めない
        log.exception("印刷用HTMLを残せませんでした %s/%s/%s", report_date, line, shift)
        out["errors"].append(str(exc))
    return out


def saved_note(result: dict) -> str:
    """`save_shift` の結果を一言に(紙の窓の帯に出す)。残さなかった(ページが無い)なら空。"""
    files, errors = result.get("files") or [], result.get("errors") or []
    if errors and not files:
        return f"印刷用HTMLを出力先へ残せませんでした: {errors[0]}"
    if not files:
        return ""
    text = f"印刷用HTMLを {len(files)}ページぶん残しました: {result.get('dir', '')}"
    if errors:
        text += f"(残せなかったページ: {'・'.join(errors)})"
    return text


def after_push(repo: Any, summary: Any) -> str:
    """共有へ送り切れた直の紙を残して、一言を返す(手で押した・自動、どちらも同じ)。

    1ページでも送れなかった直は残しません(共有に出た中身と紙を揃える)。
    """
    from ..logic import push_scope

    failed = {tuple(o.key[:3]) for o in getattr(summary, "failed", [])}
    shifts = [k for k in push_scope.oldest_first(tuple(k[:3]) for k in getattr(summary, "succeeded", []))
              if k not in failed]
    if not shifts:
        return ""
    files: list[str] = []
    errors: list[str] = []
    folders: list[str] = []
    for report_date, line, shift in dict.fromkeys(shifts):
        got = save_shift(repo, report_date, line, shift)
        files += got["files"]
        errors += [f"{report_date} {shift} {e}" for e in got["errors"]]
        if got["dir"] and got["dir"] not in folders:
            folders.append(got["dir"])
    if errors and not files:
        return f"印刷用HTMLを出力先へ残せませんでした: {errors[0]}"
    if not files:
        return ""
    text = f"印刷用HTML: {len(files)}ページを残しました({'、'.join(folders)})"
    if errors:
        text += f"。残せなかったページ: {'・'.join(errors)}"
    return text
