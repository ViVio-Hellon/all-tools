"""中身の無い紙を、見つけて片付ける

    残っているのか見えなければ消せないですよね

**探すのはDBから。** 画面が持っている値ではありません ── 別の端末で
できたぶんも、このツールより前に入ったぶんも、同じように見つかります
(`services/shift_check.py` と同じ思想)。

**消してよいかは、消す直前にもう一度確かめます。** 画面が「この紙は
空だ」と言ってきても信じません ── 一覧を出してから押すまでのあいだに
誰かが打っているかもしれないので。
"""
from __future__ import annotations

from ..db.repository import NippouRepository
from ..logging_setup import get_logger
from ..logic import empty_pages as logic
from ..logic import line_names

log = get_logger("services.empty_pages")


def find(repo: NippouRepository, *, line: str | None = None) -> logic.Found:
    """中身の無い紙をぜんぶ。**古いものから並びます。**

    `line` を渡すとそのラインだけ。渡さなければ手元にあるぜんぶです
    ── 片付けるのは据え付けの作業なので、ライン違いも見えたほうが
    片付きます。
    """
    found = []
    for key in repo.list_keys(line):
        loaded = repo.load(*key)
        if loaded is None:
            continue
        header, details = loaded
        if not logic.is_empty(details):
            continue
        found.append(logic.Page(
            report_date=header.report_date, line=header.line,
            shift=header.shift, page=header.page, worker=header.worker,
            saved_at=header.saved_at,
            # 共有へ渡してあるか。`synced_at` が入っていれば渡してある
            synced=bool(header.synced_at)))
    return logic.build(found)


def remove(repo: NippouRepository, report_date: str, line: str, shift: str,
           page: int) -> tuple[bool, str]:
    """1枚消す。(消せたか, 文言)。

    **消す直前に、もう一度中身を見ます。** 一覧を出してから押すまでの
    あいだに打たれているかもしれないので ── 画面の言うことは当てに
    しません。
    """
    loaded = repo.load(report_date, line, shift, page)
    key_text = f"{report_date} {line_names.label(line)} {shift} 第{page}ページ"
    if loaded is None:
        return False, f"{key_text} は見つかりません(もう消えています)"

    header, details = loaded
    if not logic.is_empty(details):
        log.info("打ってある紙なので消しません key=%s", key_text)
        return False, (f"{key_text} には打ってあるものがあります。"
                       "中身の無いページだけを消せます")
    if header.synced_at:
        log.info("共有へ渡してあるので消しません key=%s", key_text)
        return False, (f"{key_text} は共有へ渡してあるので、ここからは"
                       "消せません(手元だけ消すと、どちらが本当か"
                       "分からなくなります)")

    if not repo.delete_page(report_date, line, shift, page):
        return False, f"{key_text} は見つかりません(もう消えています)"

    # 集計も作り直します ── 消した紙のぶんが残っていると、グラフと
    # CSVだけ古いままになります。**空の紙なので数は変わりません**が、
    # 「ページが1枚減った」ことは反映されます
    try:
        from . import summary

        summary.refresh_shift(repo, report_date, line, shift)
    except Exception:                             # noqa: BLE001 - 消せてはいる
        log.exception("消したあとの集計の作り直しでエラー key=%s", key_text)

    log.info("中身の無い紙を消しました key=%s", key_text)
    return True, f"{key_text} を消しました"
