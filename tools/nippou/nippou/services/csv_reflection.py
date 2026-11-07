"""直しが集計CSVに入ったかを、**共有フォルダの集計CSVを読んで**確かめる (v4.46.9)

    直しが入ったかの確認は実際開いて確認しかないですよね
    → 共有フォルダの集計CSVを読み、直したあとに書き直されたかを見る仕組みを作ってください

ほかのラインの直を管理者のPCで直すと、集計CSVはそのラインのPCが直しを受け取ったときに
書き直します(`day_csv.refusal`)。それが済んだかは、CSVを開いて目で比べるしかありませんでした。

ここでは**出力先(1つ目・共有)にあるその日の集計CSV**を読み、その直の行を**このPCの中身**
(直した中身)と1行ずつ比べます。

    明細CSV   その直の行(ページ・行で突き合わせ)。全部の列を比べる
    集計CSV   その直の1行。全部の列を比べる

ファイルの日時では見ません ── 書き直されていても、古い中身で書かれていれば意味が無いので。

比べない列は **係数処理ﾛｯﾄ数** だけです。負荷係数はそのラインのPCが前の直を見て数え直す
(このPCではほかのラインの係数を数えない)ので、このPCの値と違っていて当たり前です。

食い違ったら「まだ」です。直したあとに、そのラインのPCでさらに直したときも「違う」に
なります(このPCの中身が古い)── どちらかは、出す一言に「どこが違うか」を添えて見分けます。
"""
from __future__ import annotations

import csv
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

#: 比べない列(そのラインのPCが数え直す)
SKIP_COLUMNS = frozenset({"係数処理ﾛｯﾄ数"})

#: 出す食い違いの数(多いと読めない)
MAX_DIFFS = 5

DONE = "done"
NOT_YET = "not_yet"
NO_CSV = "no_csv"
UNREADABLE = "unreadable"
TEST = "test"
EMPTY = "empty"


@dataclass
class Check:
    state: str
    text: str
    diffs: list[str] = field(default_factory=list)
    folder: str = ""

    @property
    def done(self) -> bool:
        return self.state == DONE

    def as_dict(self) -> dict:
        return {"state": self.state, "text": self.text, "diffs": list(self.diffs),
                "folder": self.folder, "done": self.done}


def _read(path: Path) -> list[dict[str, str]]:
    with open(path, encoding="utf-8-sig", newline="") as f:
        return [{k: (v or "").strip() for k, v in row.items() if k is not None}
                for row in csv.DictReader(f)]


def _same_shift(row: dict[str, str], report_date: str, line: str, shift: str) -> bool:
    return (row.get("日付", "") == str(report_date) and row.get("ライン", "") == str(line)
            and row.get("直", "") == str(shift))


def _where(row: dict[str, str]) -> str:
    return f"{row.get('ページ', '?')}ページ {row.get('行', '?')}行目"


def _compare(expected: list[dict[str, str]], found: list[dict[str, str]],
             columns: list[str], keyed: bool) -> list[str]:
    """食い違いを、人が読める形で。`keyed` なら ページ・行 で突き合わせる。"""
    diffs: list[str] = []
    if keyed:
        want = {(r.get("ページ", ""), r.get("行", "")): r for r in expected}
        got = {(r.get("ページ", ""), r.get("行", "")): r for r in found}
        for key in sorted(set(want) | set(got), key=lambda k: (_num(k[0]), _num(k[1]))):
            a, b = want.get(key), got.get(key)
            if a is None:
                diffs.append(f"{_where(b)}: 集計CSVにあるが、このPCには無い行")
            elif b is None:
                diffs.append(f"{_where(a)}: 集計CSVに無い")
            else:
                diffs.extend(f"{_where(a)} {c}: 集計CSV「{b.get(c, '')}」・このPC「{a.get(c, '')}」"
                             for c in columns
                             if c not in SKIP_COLUMNS and a.get(c, "") != b.get(c, ""))
        return diffs
    if len(expected) != len(found):
        return [f"集計の行: 集計CSV {len(found)}行・このPC {len(expected)}行"]
    for a, b in zip(expected, found):
        diffs.extend(f"集計 {c}: 集計CSV「{b.get(c, '')}」・このPC「{a.get(c, '')}」"
                     for c in columns
                     if c not in SKIP_COLUMNS and a.get(c, "") != b.get(c, ""))
    return diffs


def _num(text: str) -> int:
    try:
        return int(text)
    except (TypeError, ValueError):
        return 0


def _text_of(rows: dict[str, object]) -> dict[str, str]:
    return {k: ("" if v is None else str(v)).strip() for k, v in rows.items()}


def expected_rows(repo, report_date: str, line: str, shift: str
                  ) -> tuple[list[dict[str, str]], list[dict[str, str]]]:
    """このPCの中身から作る、その直の (明細の行, 集計の行)。CSVを書くときと同じ道で作る。"""
    from ..reporting import csv_export
    from . import summary

    # **このPCの集計を先に作り直す**(v4.46.11)。比べるもとは明細(ページ)で、集計はそこから作るもの。
    # 作り直しに失敗したまま残った古い集計と比べると、直した中身が入っていないのに「入っています」になる
    summary.refresh_shift(repo, report_date, line, shift)
    reports = [r for r in summary.for_date(repo, report_date, line) if r.shift == shift]
    lots = [r for r in summary.day_lot_rows(repo, report_date, line) if r.shift == shift]
    detail = [_text_of(r) for r in csv_export.detail_rows_of(lots, reports)]
    aggregate = [_text_of(csv_export.aggregate_row_dict(summary.as_aggregate(r)))
                 for r in reports]
    return detail, aggregate


def where_to_read(line: str, base: Optional[Path] = None) -> tuple[Path, str]:
    """そのラインの集計CSVを読む先と、読めないときの理由(読めれば空)(v4.46.11)。

    ラインのPCが**実際に書いている先**(控えのフォルダに残した `day_csv.output_dir_of`)を読む。
    出力先は端末ごとの設定なので、このPCの出力先を読むと、違っていればいつまでも「まだ」になる。

    **ほかのPCからも同じ場所を指す道(`\\\\サーバ\\共有\\…`)だけを読む。** `C:\\…` はPCごとに別の
    場所で、このPCにも同じ名前のフォルダがあると**このPC自身が前に書いたCSV**を読んで、直しが
    入っていないのに「入っています」と言いかねない(いちばん悪い間違い)。分からないときは読まずにそう言う。
    """
    from ..config import SETTINGS
    from ..db.repository import this_pc
    from . import day_csv

    own = Path(base if base is not None else SETTINGS.report_output_dir)
    if base is not None or not day_csv.refusal(line):
        return own, ""
    theirs = day_csv.output_dir_of(line)
    if theirs is None:
        # ラインのPCが前の版で、書いた先を残していない。このPCの出力先が共有なら同じ先とみて読む
        if day_csv.is_shared(day_csv.real_path(own)):
            return own, ""
        return own, (f"{line} のPCが集計CSVをどこへ書いているか、まだ分かりません"
                     f"({line} のPCを新しい版にして、1度保存(確定)すると分かります)。"
                     "このPCの出力先は共有のフォルダではないので、読んで比べません")
    there, pc = Path(theirs[0]), theirs[1]
    if not (day_csv.is_shared(theirs[0]) or (pc and pc == this_pc())):
        name = f"({pc})" if pc else ""
        return there, (f"{line} のPC{name}は集計CSVを、そのPCの中({there})に出しています。"
                       "このPCからそこを読めません(共有のフォルダにするには、"
                       f"{line} のPCの設定で集計CSVの出力先を共有のフォルダにしてください)")
    try:
        if there.is_dir():
            return there, ""
    except OSError:
        pass
    return there, (f"{line} のPCは集計CSVを {there} に出しています。このPCからそこを読めません"
                   "(このPCから届くか確かめてください)")


def why_not_yet(repo, report_date: str, line: str, shift: str) -> str:
    """「まだ」のわけ(v4.46.11)。分からなければ空。

        このPCの直しが控えに届いていない → 1分ごとに写し直す
        控えには届いている             → そのラインのPCが受け取るのを待っている
        控えのほうが新しい中身          → そのラインのPCでそのあと直された
        このPCのラインで書けていない     → 1分ごとに書き直す
    """
    from . import day_csv, local_backup

    if not day_csv.refusal(line):
        for d, ln, _at, error in day_csv.owed(repo):
            if (d, ln) == (str(report_date), str(line)):
                return f"このPCで集計CSVを書けていません(1分ごとに書き直します): {error}"
        return ""
    keys = [(report_date, line, shift, int(p)) for p in repo.saved_pages(report_date, line, shift)]
    if not keys:
        return ""
    waiting = set(map(tuple, repo.backup_pending()))
    if any(k in waiting for k in keys):
        return ("このPCの直しが、まだ控えに届いていません(1分ごとに写し直します。"
                "控えの置き場所に届くか、設定・管理者の「控え」で確かめてください)")
    try:
        with local_backup.reader(line) as backup:
            if backup is None:
                return f"{line} の控えがありません"
            there = {(report_date, line, shift, int(p))
                     for p in backup.saved_pages(report_date, line, shift)}
            other = None
            for key in sorted(set(keys) | there):
                theirs = local_backup._stamp(backup.conn, key)
                mine = local_backup._stamp(repo.conn, key)
                if theirs is None or mine is None or theirs[1] != mine[1]:
                    other = local_backup._owner(backup.conn, key) or ("", theirs[0] if theirs else "", "")
                    break
    except Exception:                             # noqa: BLE001 - わけが分からないだけ
        return ""
    if other is not None:
        # **入れ直しは勧めない。** 入れ直すとこのPCも控えの中身になり、直しが置き換えられて
        # いても「入っています」と出てしまう。まず中身を見比べてもらう
        who = f"{other[0]} が " if other[0] else ""
        when = f"{other[1][5:16].replace('T', ' ')} に" if len(other[1]) >= 16 else ""
        return (f"控えの中身が、このPCの直しと違います(控えは {who}{when}保存したもの)。"
                "このPCの直しが、そのあとの保存で置き換えられたかもしれません。"
                "「ほかのPCの日報」で控えの中身を見て確かめてください")
    return (f"控えには届いています。{line} のPCが受け取って書き直すのを待っています"
            f"({line} のPCが動いていれば5分以内・止まっていれば次に起動したとき)")


def check(repo, report_date: str, line: str, shift: str, *,
          base: Optional[Path] = None, cache: Optional[dict] = None) -> Check:
    """その直の直しが、そのラインの集計CSV(ラインのPCが書いている先)に入っているか。"""
    from ..reporting import csv_export

    out_dir, unreadable = where_to_read(line, base)
    paths = csv_export.daily_output_paths(out_dir, report_date, line)
    folder = str(paths["aggregate"].parent)
    if repo.is_test_shift(report_date, line, shift):
        return Check(TEST, "テストの直なので集計CSVに出しません", folder=folder)
    if unreadable:
        return Check(UNREADABLE, unreadable, folder=folder)
    detail, aggregate = expected_rows(repo, report_date, line, shift)
    missing = [p for key, p in paths.items() if key in ("aggregate", "detail") and not p.exists()]
    if missing:
        if not detail and not aggregate:
            return Check(EMPTY, "打った行が無い直です(集計CSVに出る行がありません)", folder=folder)
        why = why_not_yet(repo, report_date, line, shift)
        return Check(NO_CSV, f"まだです: {why}" if why else "まだです",
                     diffs=[f"集計CSVがまだありません: {missing[0].name}"], folder=folder)
    # 一覧では同じ日の直が並ぶ。共有のファイルは1日1回だけ読む
    cache = {} if cache is None else cache

    def read(path: Path) -> list[dict[str, str]]:
        if path not in cache:
            cache[path] = _read(path)
        return cache[path]

    try:
        found_detail = [r for r in read(paths["detail"])
                        if _same_shift(r, report_date, line, shift)]
        found_aggregate = [r for r in read(paths["aggregate"])
                           if _same_shift(r, report_date, line, shift)]
    except (OSError, UnicodeError, csv.Error) as exc:
        return Check(UNREADABLE, f"集計CSVを読めません: {exc}", folder=folder)
    diffs = (_compare(detail, found_detail, csv_export.DETAIL_FIELDNAMES, keyed=True)
             + _compare(aggregate, found_aggregate, csv_export.FIELDNAMES, keyed=False))
    if not diffs:
        return Check(DONE, "入っています", folder=folder)
    shown = diffs[:MAX_DIFFS]
    if len(diffs) > MAX_DIFFS:
        shown.append(f"ほか {len(diffs) - MAX_DIFFS}件")
    why = why_not_yet(repo, report_date, line, shift)
    text = f"まだです(違うところ {len(diffs)}件)" + (f": {why}" if why else "")
    return Check(NOT_YET, text, diffs=shown, folder=folder)


def check_many(repo, keys, *, base: Optional[Path] = None) -> list[dict]:
    """一覧の直をまとめて。1つ読めなくても残りは続ける。"""
    from ..logging_setup import get_logger

    out: list[dict] = []
    cache: dict = {}
    for report_date, line, shift in keys:
        try:
            got = check(repo, report_date, line, shift, base=base, cache=cache)
        except Exception as exc:                  # noqa: BLE001 - 一覧は出す
            get_logger("services.csv_reflection").exception(
                "集計CSVと比べられませんでした %s/%s/%s", report_date, line, shift)
            got = Check(UNREADABLE, f"比べられませんでした: {exc}")
        out.append({"report_date": report_date, "line": line, "shift": shift, **got.as_dict()})
    return out
