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
    # **短く・することが分かる言い方で**(「文章が難解すぎる」)。理由はこのモジュールの説明に
    # 集計CSVは**その日の直ぜんぶ**の1本。最新の直ぜんぶを持っているのはそのラインのPCだけなので、
    # そのラインのPCが直しを受け取ったときに書き直す(1.5.93 で控えと合わせて書いたが、控えが
    # そのラインのPCより古いと古い中身で上書きし、誰も直さないことがあったので戻した)
    return (f"{line} の集計CSVは、{line} のPCが直しを受け取ったときに書き直します"
            f"({line} のPCが動いていれば5分以内・止まっていれば次に起動したとき)。"
            f"入ったかは「記録を見る」の「集計CSV」の欄で見られます")


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
    if factors:
        # **あとの直の係数も**(前の直から続くロットの見分けが変わる。v4.46.8)── この端末のラインだけ。
        # 書き換わったあとの直も、集計と集計CSVを作り直す
        following: set[tuple[str, str, str]] = set()
        for report_date, line, shift in keys:
            if refusal(line):
                continue
            # **落ちても集計CSVまで進む**(v4.46.11)。ここで抜けると、取り込んだ直しが集計CSVに
            # 出ないまま「取り込み済み」になり、二度と書かれない
            try:
                if repo.saved_pages(report_date, line, shift):
                    load_factor.recalculate(repo, report_date, line, shift)
                following.update(load_factor.recalculate_following(repo, report_date, line, shift))
            except Exception as exc:              # noqa: BLE001 - 明細は保存済み
                log.exception("負荷係数を数え直せませんでした %s/%s/%s", report_date, line, shift)
                notes.append(f"{report_date} {line} {shift} の負荷係数を数え直せませんでした: {exc}")
        keys = sorted(set(keys) | following)
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
            written = write_day(repo, report_date, line, base)
        except Exception as exc:                  # noqa: BLE001 - 覚えておいて書き直す(`retry_owed`)
            log.exception("集計CSVを書き直せませんでした %s/%s", report_date, line)
            notes.append(f"{report_date} {line} の集計CSVを書き直せませんでした"
                         f"(1分ごとに書き直します): {exc}")
            continue
        log.info("集計CSVを書き直しました %s/%s → %s", report_date, line, written.folder)
        notes.append(f"{report_date} {line} の集計CSVを {written.folder} に書き直しました")
    return notes


# ----------------------------------------------------------------------
# 書けなかった日を覚えて書き直す (v4.46.11)
#
#     管理者が修正後、現地PCからの開きで集計CSVが直るか … 一連のチェック穴がないかのチェック
#
# ラインのPCは、管理者の直しを控えから取り込んだときにその日の集計CSVを書き直します。そのとき
# 出力先(共有)に届かず書けないと、取り込みは済んでいるので**次の取り込みには何も無く、二度と
# 書かれません**でした(集計CSVは直す前のまま)。書けなかった日はこの端末のDBに覚えておき
# (`csv_owed`)、1分ごとの見張り(`/api/entry/close`)と起動のときに書き直します。書けたら消します。
# ----------------------------------------------------------------------
OWED_TABLE = "csv_owed"


def _owed_table(conn) -> None:
    conn.execute(f"CREATE TABLE IF NOT EXISTS {OWED_TABLE} ("
                 " report_date TEXT NOT NULL, line TEXT NOT NULL, at TEXT, error TEXT,"
                 " PRIMARY KEY (report_date, line))")


def owe(repo, report_date: str, line: str, error: str) -> None:
    """その日の集計CSVを書けなかった(あとで書き直す)。"""
    from datetime import datetime

    try:
        with repo.conn:
            _owed_table(repo.conn)
            repo.conn.execute(
                f"INSERT OR REPLACE INTO {OWED_TABLE} (report_date, line, at, error) VALUES (?,?,?,?)",
                (str(report_date), str(line), datetime.now().isoformat(timespec="seconds"),
                 str(error)[:500]))
    except Exception:                             # noqa: BLE001 - 覚えられなくても続ける
        from ..logging_setup import get_logger
        get_logger("services.day_csv").exception("書けなかった日を覚えられませんでした %s/%s",
                                                 report_date, line)


def paid(repo, report_date: str, line: str) -> None:
    """その日の集計CSVを書けた(覚えていれば消す)。"""
    try:
        with repo.conn:
            _owed_table(repo.conn)
            repo.conn.execute(f"DELETE FROM {OWED_TABLE} WHERE report_date=? AND line=?",
                              (str(report_date), str(line)))
    except Exception:                             # noqa: BLE001
        pass


def owed(repo) -> list[tuple[str, str, str, str]]:
    """書けていない日: (日付, ライン, 覚えた時刻, 理由)。"""
    try:
        _owed_table(repo.conn)
        return [tuple(str(v or "") for v in r) for r in repo.conn.execute(
            f"SELECT report_date, line, at, error FROM {OWED_TABLE} ORDER BY at")]
    except Exception:                             # noqa: BLE001
        return []


def write_day(repo, report_date: str, line: str, base=None):
    """その日の集計CSV 3本を書く。**どの道もここを通す**(書けなければ覚える・書けたら消す・
    どこへ書いたかを控えに残す)。書けなければ例外をそのまま上げる。"""
    from ..config import SETTINGS
    from ..reporting import csv_export

    out_dir = base if base is not None else SETTINGS.report_output_dir
    # 覚える・消すも**書くのと同じ錠の中で**(錠の外だと、あとから書けなかった回の「覚える」を、
    # 先に書けた回の「消す」が消してしまう)
    with _lock if _lock is not None else nullcontext():
        try:
            written = csv_export.write_daily_set(repo, report_date, line, out_dir)
        except Exception as exc:
            owe(repo, report_date, line, str(exc))
            raise
        paid(repo, report_date, line)
    if base is None:
        note_output_dir(line, out_dir)
    return written


def retry_owed(repo) -> list[str]:
    """書けていない日を書き直す(1分ごとの見張り・起動のとき)。返すのは書き直した・まだ書けない一言。"""
    from ..logging_setup import get_logger

    log = get_logger("services.day_csv")
    notes: list[str] = []
    for report_date, line, _at, _error in owed(repo):
        own = owner_line()
        if not own:
            continue                              # この端末のラインがまだ分からない(消さずに残す)
        if own != line:
            paid(repo, report_date, line)         # この端末のラインではなくなった(書くのはそのPC)
            continue
        from ..config import SETTINGS
        if (not _has_production(repo, report_date, line)
                and not _csv_exists(SETTINGS.report_output_dir, report_date, line)):
            paid(repo, report_date, line)         # テストの直だけの日(本番のCSVは出さない ── `settle` と同じ)
            continue
        try:
            written = write_day(repo, report_date, line)
        except Exception as exc:                  # noqa: BLE001 - 次の見張りでまた
            log.warning("集計CSVをまだ書けません %s/%s: %s", report_date, line, exc)
            notes.append(f"{report_date} {line} の集計CSVをまだ書けません: {exc}")
            continue
        log.info("書けていなかった集計CSVを書き直しました %s/%s → %s", report_date, line, written.folder)
        notes.append(f"{report_date} {line} の集計CSVを {written.folder} に書き直しました")
    return notes


# ----------------------------------------------------------------------
# このラインの集計CSVをどこへ書いているか ── 控えのフォルダに残す (v4.46.11)
#
# 出力先は端末ごとの設定です(既定はそのPCの中)。管理者のPCが「集計CSVに入ったか」を見るとき、
# 自分の出力先を読むと、ラインのPCの出力先と違えば**いつまでも「まだ」**になります。ラインのPCは
# 書いた先を控えのフォルダ(`LocalBackup\<ライン>\`)に残し、管理者のPCはそこを読みます
# (`csv_reflection`)。
# ----------------------------------------------------------------------
WHERE_FILE = "集計CSVの出力先.json"


def where_file(line: str):
    from . import local_backup

    return local_backup.path_for(line).parent / WHERE_FILE


def note_output_dir(line: str, out_dir) -> None:
    """このラインの集計CSVを `out_dir` に書いた、と控えのフォルダに残す(変わったときだけ書く)。"""
    import json
    from datetime import datetime

    from ..db.repository import this_pc
    from . import local_backup

    try:
        if not local_backup.reachable():
            return
        path = where_file(line)
        if not path.parent.is_dir():
            return                                # 控えがまだ無い(作るのは控えへ写すとき)
        try:
            known = json.loads(path.read_text(encoding="utf-8")).get("dir")
        except (OSError, ValueError, AttributeError):
            known = None
        if known == str(out_dir):
            return
        path.write_text(json.dumps({"dir": str(out_dir), "real": real_path(out_dir), "pc": this_pc(),
                                    "at": datetime.now().isoformat(timespec="seconds")},
                                   ensure_ascii=False), encoding="utf-8")
    except Exception:                             # noqa: BLE001 - 残せなくても書いたCSVはそのまま
        from ..logging_setup import get_logger
        get_logger("services.day_csv").exception("集計CSVの出力先を控えに残せませんでした %s", line)


def real_path(path) -> str:
    """ほかのPCから見ても同じ場所を指す形。割り当てたドライブ(Z: など)は Windows では
    `\\\\サーバ\\共有\\…` になる(`os.path.realpath`)。たどれなければそのまま。"""
    import os

    try:
        return os.path.realpath(str(path))
    except (OSError, ValueError):
        return str(path)


def is_shared(text: str) -> bool:
    """ほかのPCからも同じ場所を指す道か(`\\\\サーバ\\共有\\…`)。`C:\\…`・`Z:\\…` は PC ごとに違う。"""
    return str(text).replace("/", "\\").startswith("\\\\")


def output_dir_of(line: str) -> "tuple[str, str] | None":
    """そのラインのPCが集計CSVを書いている先 (ほかのPCから見た形, PC名)。分からなければ None。"""
    import json

    try:
        got = json.loads(where_file(line).read_text(encoding="utf-8"))
        where = got.get("real") or got.get("dir")
        return (str(where), str(got.get("pc", ""))) if where else None
    except Exception:                             # noqa: BLE001 - 無い・読めない
        return None


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
