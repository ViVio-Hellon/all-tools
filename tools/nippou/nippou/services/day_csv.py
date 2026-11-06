"""その日の集計CSVを**書いてよい端末か** ── 一部の中身で上書きしない関門 (v4.46.3)

集計CSV(日・ラインごとの3本)は、**その日・そのラインの直ぜんぶ**を手元の日報から作り、
出力先へ上書きします。手元にそのラインの直が全部あるのは、**そのラインの端末**だけです。

    管理者のPCで、ほかのライン(機側)の前日2直を控えから呼び出して直す
      → 管理者のPCの手元にある前日の機側は、その2直だけ
      → 保存で前日の機側の集計CSVを作り直すと、出力先(共有のフォルダ)の3本が
        **その2直だけの中身で上書き**され、機側の端末が書いた1直・3直が黙って消える

月替わりの書き出しは同じ穴を `month_rollover._partial_lines` で塞いでいます。日の集計CSVにも
同じ関門を置きます。**書くのはそのラインの端末だけ**です。ほかのラインの直しは、そのラインの
端末が直しを控えから取り込んだときに、その端末が直ぜんぶで作り直します(`local_backup`)。

この端末のラインを決めていない端末(ライン未設定の管理者のPCなど)は、どのラインも「自分の
ライン」ではないので書きません。
"""
from __future__ import annotations

import threading
from contextlib import nullcontext

#: 書き込みの錠(`app/__init__.py` の `_WRITE_LOCK`。起動のときに `use_lock` で渡す)。
#: 控えへ写す処理は応答のあと(錠の外)で走るので、そこから CSV を書くときに保存の要求と
#: 同じ3本を同時に書かないよう、同じ錠を取る(RLock なので要求の中から呼んでもよい)
_lock: "threading.RLock | None" = None


def use_lock(lock) -> None:
    global _lock
    _lock = lock


def owner_line() -> str:
    """この端末のライン(決めていなければ空)。"""
    from .. import work_context

    return (work_context.stored_terminal_line()
            if work_context.terminal_line_decided() else "")


def refusal(line: str) -> str:
    """そのラインの日の集計CSVを、この端末で書いてはいけない理由。**書いてよければ空。**"""
    own = owner_line()
    if line and line == own:
        return ""
    return (f"{line} の集計CSVはこの端末では書き直しません(この端末には {line} のその日の直が"
            f"全部はないので、書くと {line} の端末が書いた直が消えます)。"
            f"{line} の端末が直しを取り込んだときに、直ぜんぶで書き直します")


def settle(repo, shifts, *, factors: bool = True, base=None) -> list[str]:
    """ページを書き換えた・消した直を、**保存(確定)と同じところまで**揃える(v4.46.3)。

        1. 負荷係数を数え直す(`factors`。機側・NS1・AIM だけ効く)
        2. その直の集計を作り直す(ページが無くなった直は集計を消す)
        3. その日の集計CSV 3本を、出力先(1つ目)へ**その日の直ぜんぶで**書き直す
           ── ほかのラインの日は書かない(`refusal`)。テストの直だけの日は書かない

    「前日分を修正した。印刷は更新していたが、集計ファイルは更新していない」── 直す道ごとに
    やることがばらばらで、集計CSVを書き直さない道が残っていました。直す道はここを通します。
    **1つ失敗しても残りは続けます**(直した明細は保存済み。理由は返す文と記録に残す)。
    返すのは画面に添える一言(書き直した・書かなかった理由・失敗)。
    """
    from ..config import SETTINGS
    from ..logging_setup import get_logger
    from ..reporting import csv_export
    from . import load_factor, summary

    log = get_logger("services.day_csv")
    keys = sorted({(str(d), str(ln), str(sh)) for d, ln, sh in shifts})
    notes: list[str] = []
    for report_date, line, shift in keys:
        # 負荷係数は**この端末のラインだけ**数え直す(ほかのラインは前の直が手元に揃って
        # いないことがあり、数え直すと直をまたいだロットを二重に数えて書き換えてしまう)
        if factors and not refusal(line) and repo.saved_pages(report_date, line, shift):
            try:
                load_factor.recalculate(repo, report_date, line, shift)
            except Exception as exc:              # noqa: BLE001 - 明細は保存済み
                log.exception("負荷係数を数え直せませんでした %s/%s/%s", report_date, line, shift)
                notes.append(f"{report_date} {line} {shift} の負荷係数を数え直せませんでした: {exc}")
        try:
            summary.refresh_shift(repo, report_date, line, shift)
        except Exception as exc:                  # noqa: BLE001
            log.exception("集計を作り直せませんでした %s/%s/%s", report_date, line, shift)
            notes.append(f"{report_date} {line} {shift} の集計を作り直せませんでした: {exc}")
    tests = repo.test_shift_keys()
    days: dict[tuple[str, str], list[str]] = {}
    for report_date, line, shift in keys:
        days.setdefault((report_date, line), []).append(shift)
    out_dir = base if base is not None else SETTINGS.report_output_dir
    for (report_date, line), changed in sorted(days.items()):
        if all((report_date, line, sh) in tests for sh in changed):
            continue                              # テストの直だけ(本番の CSV は変わらない)
        if not _has_production(repo, report_date, line) and not _csv_exists(out_dir, report_date, line):
            continue                              # 本番の直が無く、前に出したCSVも無い(テストの直を消しただけ)
        refused = refusal(line)
        if refused:
            log.info("集計CSVを書きませんでした(ほかのライン) %s/%s", report_date, line)
            notes.append(refused)
            continue
        try:
            with _lock if _lock is not None else nullcontext():
                written = csv_export.write_daily_set(repo, report_date, line, out_dir)
        except Exception as exc:                  # noqa: BLE001
            log.exception("集計CSVを書き直せませんでした %s/%s", report_date, line)
            notes.append(f"{report_date} {line} の集計CSVを書き直せませんでした: {exc}")
            continue
        log.info("集計CSVを書き直しました %s/%s → %s", report_date, line, written.folder)
        notes.append(f"{report_date} {line} の集計CSVを {written.folder} に書き直しました")
    return notes


def _has_production(repo, report_date: str, line: str) -> bool:
    """その日・そのラインに、テストではない直のページがあるか。"""
    tests = repo.test_shift_keys(line)
    return any((k[0], k[1], k[2]) not in tests
               for k in repo.list_keys(line) if k[0] == report_date)


def _csv_exists(base, report_date: str, line: str) -> bool:
    from ..reporting import csv_export

    return any(p.exists() for p in csv_export.daily_output_paths(base, report_date, line).values())


def problems(notes: list[str]) -> list[str]:
    """`settle` の一言のうち、**利用者に見せるべきもの**(書かなかった理由・失敗)。"""
    return [n for n in notes if "書き直しました" not in n]
