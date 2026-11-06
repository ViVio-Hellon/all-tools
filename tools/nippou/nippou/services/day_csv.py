"""その日の集計CSVを**書いてよい端末か** ── 一部の中身で上書きしない関門 (v4.46.3)

集計CSV(日・ラインごとの3本)は、**その日・そのラインの直ぜんぶ**を手元の日報から作り、
出力先へ上書きします。手元にそのラインの直が全部あるのは、**そのラインの端末**だけです。

    管理者のPCで、ほかのライン(機側)の前日2直を控えから呼び出して直す
      → 管理者のPCの手元にある前日の機側は、その2直だけ
      → 保存で前日の機側の集計CSVを作り直すと、出力先(共有のフォルダ)の3本が
        **その2直だけの中身で上書き**され、機側の端末が書いた1直・3直が黙って消える

月替わりの書き出しは同じ穴を `month_rollover._partial_lines` で塞いでいます。日の集計CSVにも
同じ関門を置きます。

**ほかのラインの日は、そのラインの控え(LocalBackup)と合わせて書きます**(v4.46.6 `merged_day`)。
控えにはそのラインの端末が保存した直が全部あるので、それをメモリーへ写し、この端末で直した直
だけを手元の中身に差し替えて、**その日の直ぜんぶ**で書きます(「管理者PCで共有へ送るタイミング
では集計は直せないのですか？」)。控えを読めないときは書かず、そのラインの端末が直しを控えから
取り込んだときに書き直します(`local_backup`)。

この端末のラインを決めていない端末(ライン未設定の管理者のPCなど)は、どのラインも「自分の
ライン」ではないので書きません。
"""
from __future__ import annotations

import sqlite3
import threading
from contextlib import contextmanager, nullcontext
from typing import Iterator, Optional

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
    # **短く・することが分かる言い方で**(「文章が難解すぎる」)。理由はこのモジュールの説明に
    return f"{line} の集計CSVは、{line} のPCが自動で更新します(このPCでは書きません)"


class NoBackup(RuntimeError):
    """そのラインの控えが無い・読めない(その日の直が揃わない)。"""


@contextmanager
def merged_day(repo, report_date: str, line: str) -> Iterator[object]:
    """ほかのラインのその日を、**そのラインの控え + この端末で直した直**で揃えたリポジトリ(メモリー)。

    控えをメモリーへ写し、この端末の手元にあるその日の直(呼び出して直した直)だけを手元の
    中身に差し替え、その日の直の集計を作り直してから渡す。控えが無い・読めなければ `NoBackup`。
    """
    from ..db.repository import NippouRepository
    from ..db.schema import ensure_schema
    from . import local_backup, summary

    mem = sqlite3.connect(":memory:")
    try:
        try:
            with local_backup.reader(line) as backup:
                if backup is None:
                    raise NoBackup(f"{line} の控えがありません")
                backup.conn.backup(mem)
        except NoBackup:
            raise
        except Exception as exc:                  # noqa: BLE001 - 届かない・読めない
            raise NoBackup(f"{line} の控えを読めませんでした({exc})") from exc
        mem.row_factory = sqlite3.Row
        mem.execute("PRAGMA foreign_keys=ON")
        ensure_schema(mem)
        merged = NippouRepository(mem)
        mine = sorted({k[2] for k in repo.list_keys(line) if k[0] == report_date})
        for shift in mine:
            # この端末で直した直は、手元の中身で置き換える(控えにまだ届いていなくても)
            with mem:
                mem.execute("DELETE FROM daily_detail WHERE report_date=? AND line=? AND shift=?",
                            (report_date, line, shift))
                mem.execute("DELETE FROM daily_header WHERE report_date=? AND line=? AND shift=?",
                            (report_date, line, shift))
            test = repo.is_test_shift(report_date, line, shift)
            for page in repo.saved_pages(report_date, line, shift):
                loaded = repo.load(report_date, line, shift, page)
                if loaded is not None:
                    merged.save(*loaded, test=test)
        # 控えにあった集計は使わず、その日の直を**明細から作り直す**(古い集計を持ち込まない)
        with mem:
            mem.execute("DELETE FROM packing_report WHERE work_date=? AND line_name=?",
                        (report_date, line))
        for shift in sorted({k[2] for k in merged.list_keys(line) if k[0] == report_date}):
            summary.refresh_shift(merged, report_date, line, shift)
        yield merged
    finally:
        mem.close()


def write_other_line(repo, report_date: str, line: str, base) -> tuple[bool, str]:
    """ほかのラインの日の集計CSVを、控えと合わせて**その日の直ぜんぶ**で書く。(書けたか, 一言)。"""
    from ..logging_setup import get_logger
    from ..reporting import csv_export

    log = get_logger("services.day_csv")
    try:
        with merged_day(repo, report_date, line) as merged:
            with _lock if _lock is not None else nullcontext():
                written = csv_export.write_daily_set(merged, report_date, line, base)
    except NoBackup as exc:
        log.info("ほかのラインの集計CSVを書けませんでした(控え) %s/%s: %s", report_date, line, exc)
        return False, (f"{line} の集計CSVはまだ更新していません({exc})。"
                       f"{line} のPCが直しを取り込んだときに自動で更新します")
    except Exception as exc:                      # noqa: BLE001
        log.exception("ほかのラインの集計CSVを書けませんでした %s/%s", report_date, line)
        return False, f"{report_date} {line} の集計CSVを書き直せませんでした: {exc}"
    log.info("ほかのラインの集計CSVを控えと合わせて書きました %s/%s → %s",
             report_date, line, written.folder)
    return True, f"{line} の集計CSVを、その日の直ぜんぶで書き直しました({written.folder})"


def settle(repo, shifts, *, factors: bool = True, base=None) -> list[str]:
    """ページを書き換えた・消した直を、**保存(確定)と同じところまで**揃える(v4.46.3)。

        1. 負荷係数を数え直す(`factors`。機側・NS1・AIM だけ効く)
        2. その直の集計を作り直す(ページが無くなった直は集計を消す)
        3. その日の集計CSV 3本を、出力先(1つ目)へ**その日の直ぜんぶで**書き直す
           ── ほかのラインの日は、そのラインの控えと合わせて書く(`write_other_line`)。テストの直だけの日は書かない

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
        if refusal(line):
            # ほかのラインの日は、そのラインの控えと合わせて書く(`merged_day`)
            _ok, text = write_other_line(repo, report_date, line, out_dir)
            notes.append(text)
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
    return [n for n in notes if "書き直しました" not in n or "書き直せません" in n]
