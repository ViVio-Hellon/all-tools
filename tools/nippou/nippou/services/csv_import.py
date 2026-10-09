"""過去の日報を取り込む ── **押すまで書かない**

【なぜ下見を挟むのか】
取り込みは、**いま入っているものを黙って置き換えられる**操作です。
`repo.save` は同じ(日付・ライン・直・ページ)を丸ごと入れ替えるので、
打ち間違えたファイルを選んだだけで今日の12行が消えます。

だから2段にします。

    下見 (`preview`)  … 何件入るか / どれが既存を置き換えるか を出す。**書かない**
    実行 (`apply`)    … 下見で見た内容を、そのまま入れる

【共有へは送りません(既定)】
`repo.save` は送信待ち(dirty)にします。過去のぶんをそのまま入れると、
次の「共有へ保存」で**何か月ぶんもが一斉に Access へ飛びます。**
古いデータは VBA の時代に既に入っているのが普通なので、取り込んだものは
**送信済みとして印を付ける**のを既定にしました。送りたいときだけ外します。

【入れたら集計も作り直し、CSVも書く ── 保存と同じ扱いにする】
取り込みは**保存**です。人が12行打って「保存(確定)」を押したときと
同じところまで進めます:

    日報を入れる → その直の集計を作り直す → その日の集計CSVを書く

途中で止めると、置き場所によって中身が食い違います ── 紙とグラフには
出るのに、共有フォルダのCSVにはその日が無い、という状態です。
**CSVはその日報の作業日のフォルダ**(`<出力先>/<ライン>/集計/yyyy.mm/dd`)へ
出します。
取り込んだ日のぶんなので、今日のフォルダではありません。

出し先へ届かないことは普通にあります(共有が落ちている等)。そのときも
**取り込みそのものは成功のまま**にして、CSVが出なかったことだけを
知らせます ── 日報が入らないほうが困ります。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from ..db.repository import NippouRepository
from ..logging_setup import get_logger
from . import page_writer
from ..logic import csv_import
from ..logic.csv_import import Page, Parsed
from ..logic import line_names

log = get_logger("services.csv_import")

#: 1日ぶんに出るCSVの本数(集計・明細・停止内訳)。式の説明(計算内容.csv)は
#: 年月のフォルダに1つなので数えません。
#: **数の出どころを1つにする** ── 下見と結果で本数が食い違わない
CSV_PER_DAY = 3


@dataclass
class Target:
    """入れようとしている1ページ。**既にあるかどうかを添えます。**"""

    label: str
    rows: int
    replaces: bool = False        # 既にあるものを置き換える
    existing_rows: int = 0        # そのとき消える行数

    def as_dict(self) -> dict:
        return {"label": self.label, "rows": self.rows,
                "replaces": self.replaces,
                "existing_rows": self.existing_rows}


@dataclass
class Preview:
    """下見の結果。**これを見せてから押させます。**"""

    parsed: Parsed
    targets: list[Target] = field(default_factory=list)
    source: str = ""
    #: 入れる先として使ったライン(人が選んだもの、または当たり)
    line: str = ""
    #: xlsx のとき、シートの頭に書いてあったこと
    sheet: Optional[object] = None
    #: CSVの出し先(`<ここ>/<ライン>/集計/yyyy.mm/dd` へ日ごとに出ます)
    csv_dir: str = ""
    #: 入れると**消える**古いページ ``(作業日, ライン, 直, ページ)``。その直は今度のファイルの
    #: ページ数で足りるのに、前の取り込みで余分に増えていたもの(直したあとの入れ直し)
    stale: list[tuple[str, str, str, int]] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.parsed.ok

    @property
    def replacing(self) -> list[Target]:
        return [t for t in self.targets if t.replaces]

    @property
    def days(self) -> list[tuple[str, str]]:
        """CSVを書くことになる (作業日, ライン)。**日ごとに1組**。

        1つのシートに3直が入っているので、ページの数とは一致しません。
        押す前に「何日ぶん書くか」を言うために出します ── 何か月ぶんも
        まとめて取り込むと、共有フォルダに何百本も書くことになります。
        """
        seen = {(page.key[0], page.key[1]) for page in self.parsed.pages}
        return sorted(seen)

    @property
    def message(self) -> str:
        if self.parsed.missing_columns:
            missing = " / ".join(self.parsed.missing_columns)
            return (f"見出しが足りません({missing})。"
                    "1行目に見本の見出しを入れてください")
        if self.parsed.problems and not self.parsed.pages:
            return self.parsed.problems[0].reason
        if not self.targets:
            return "入れられる行がありませんでした"
        text = f"{len(self.targets)}ページ {self.parsed.row_count}行 を入れます"
        if self.replacing:
            text += f"。うち {len(self.replacing)}ページ は今あるものを置き換えます"
        if self.stale:
            text += (f"。前の取り込みで増えていた {len(self.stale)}ページ("
                     + "・".join(f"{d} {s} {p}ページ" for d, _l, s, p in self.stale[:5])
                     + (" …" if len(self.stale) > 5 else "") + ")は消します")
        if self.parsed.notes:
            text += "。" + " / ".join(self.parsed.notes)
        if self.parsed.problems:
            text += f"(読めなかった行が {len(self.parsed.problems)}行)"
        # **押す前に、書くものを全部言う。** 集計CSVも一緒に出ます
        days = self.days
        if days and self.csv_dir:
            text += (f"。集計CSVも {len(days)}日ぶん"
                     f"({len(days) * CSV_PER_DAY}本)を {self.csv_dir} の"
                     "ライン名/集計/年月/日 のフォルダへ出します")
        return text

    def as_dict(self) -> dict:
        return {"ok": self.ok, "source": self.source,
                "message": self.message,
                "line": self.line,
                "sheet": self.sheet.as_dict() if self.sheet else None,
                "targets": [t.as_dict() for t in self.targets],
                "replacing": len(self.replacing),
                "stale": [{"report_date": d, "line": l, "shift": s, "page": p}
                          for d, l, s, p in self.stale],
                "csv_dir": self.csv_dir,
                "csv_days": [{"report_date": d, "line": l} for d, l in self.days],
                **self.parsed.as_dict()}


@dataclass
class Result:
    """入れた結果。**やったことを全部持ちます。**"""

    pages: int = 0
    rows: int = 0
    replaced: int = 0
    #: 消した古いページ(前の取り込みで増えていたもの)
    removed: int = 0
    summaries: int = 0
    marked_synced: bool = True
    failed: list[tuple[str, str]] = field(default_factory=list)
    #: 集計CSVを書けた日数と本数。**保存と同じところまで進める**
    csv_days: int = 0
    csv_files: int = 0
    csv_dir: str = ""
    #: CSVだけ書けなかったぶん(日報は入っている)
    csv_failed: list[tuple[str, str]] = field(default_factory=list)

    @property
    def message(self) -> str:
        if not self.pages:
            return "入れるものがありませんでした"
        text = f"{self.pages}ページ {self.rows}行 を取り込みました"
        if self.replaced:
            text += f"(うち {self.replaced}ページ は置き換え)"
        if self.removed:
            text += f"。前の取り込みで増えていた {self.removed}ページ は消しました"
        text += "。共有へは" + ("送りません" if self.marked_synced else "送ります")
        if self.csv_days:
            text += (f"。集計CSVは {self.csv_days}日ぶん {self.csv_files}本を "
                     f"{self.csv_dir} に出しました")
        if self.csv_failed:
            # **日報は入っています。** CSVが出なかったことだけを言う
            text += (f" ── ただし {len(self.csv_failed)}日ぶんのCSVは"
                     "出せませんでした(日報は入っています)")
        if self.failed:
            text += f" ── 入らなかったものが {len(self.failed)}ページあります"
        return text

    def as_dict(self) -> dict:
        return {"pages": self.pages, "rows": self.rows,
                "replaced": self.replaced, "summaries": self.summaries,
                "marked_synced": self.marked_synced,
                "failed": [[label, why] for label, why in self.failed],
                "csv_days": self.csv_days, "csv_files": self.csv_files,
                "csv_dir": self.csv_dir,
                "csv_failed": [[label, why] for label, why in self.csv_failed],
                "message": self.message}


def _targets(repo: NippouRepository, pages: list[Page]) -> list[Target]:
    """入る先を調べる。**読むだけで、書きません。**"""
    out: list[Target] = []
    for page in pages:
        existing = repo.load(*page.key)
        out.append(Target(
            label=page.label, rows=len(page.details),
            replaces=existing is not None,
            existing_rows=len(existing[1]) if existing else 0))
    return out


def _stale(repo: NippouRepository, pages: list[Page]) -> list[tuple[str, str, str, int]]:
    """入れたあとに残ってしまう古いページ。**今度のファイルのページ数を超えるぶん。**

    ページごとの上書きなので、前の取り込みのほうがページが多いと、余りが残ります
    (過去日報の3直の行を2直へ入れていたころの取り込みを、直したあと入れ直すとき)。
    """
    most: dict[tuple[str, str, str], int] = {}
    for page in pages:
        key = page.key[:3]
        most[key] = max(most.get(key, 0), int(page.key[3]))
    out = []
    for key, top in sorted(most.items()):
        out.extend((*key, p) for p in repo.saved_pages(*key) if p > top)
    return out


def is_workbook(path: Path) -> bool:
    """VBAの日付シート(xlsx)か。**中身ではなく拡張子で決めます** ──
    開いてみるまで分からない、では下見の前にラインを聞けません。"""
    return path.suffix.lower() in (".xlsx", ".xlsm")


def _parse(path: Path, line: str,
           starts: Optional[dict[str, int]] = None) -> tuple[Parsed, Optional[object], str]:
    """読む。**xlsx は選ばれたラインで、CSVは書いてあるライン**で。

    CSVでもラインを選べば、そちらを入れる先にします(書いてある名前が
    ツールの呼び名と違うファイル向け)。選ばなければCSVの列に従います
    (前の名前 `LS` などは正規の呼び名へ揃えて ── `logic/csv_import`)。
    """
    line = line_names.upgrade(line.strip())
    if is_workbook(path):
        from ..logic import nippou_sheet

        head = nippou_sheet.read_file_head(path)
        target = line.strip() or nippou_sheet.guess_line(head.line_text)
        return nippou_sheet.parse_file(path, target, starts), head, target

    parsed = csv_import.parse_file(path)
    if line.strip() and parsed.ok:
        _retarget(parsed, line.strip())
    return parsed, None, line.strip()


def _retarget(parsed: Parsed, line: str) -> None:
    """読んだものを、選ばれたラインへ付け替える。"""
    for page in parsed.pages:
        page.header.line = line
        for detail in page.details:
            detail.line = line


def _out_dir(given: Optional[Path]) -> Path:
    """集計CSVの出し先。**出どころは設定1つ**(日報入力の保存と同じ)。"""
    if given is not None:
        return given
    from ..config import SETTINGS

    return SETTINGS.report_output_dir


def preview(repo: NippouRepository, path: Path, line: str = "", *,
            out_dir: Optional[Path] = None) -> Preview:
    """ファイルを読んで、何が起きるかを出す。**書きません。**"""
    from .. import job_progress
    from ..logic import progress as progress_logic

    # 読むところも進み具合に出します(何本もあると、ここでも待ちます)
    job_progress.step(phase=progress_logic.PHASE_READ, label=Path(path).name)
    parsed, head, target = _parse(path, line, _starts(repo))
    if parsed.ok and is_workbook(Path(path)):
        parsed.notes.extend(_date_notes(Path(path), head))
        parsed.notes.extend(_shrink_notes(repo, parsed.pages))
    return Preview(parsed=parsed, source=str(path), line=target, sheet=head,
                   csv_dir=str(_out_dir(out_dir)),
                   targets=_targets(repo, parsed.pages) if parsed.ok else [],
                   # 1日ぶん3直そろったシート(xlsx)のときだけ。CSVは一部のページだけのこともある
                   stale=_stale(repo, parsed.pages) if parsed.ok and is_workbook(Path(path)) else [])


def file_name_date(name: str):
    """ファイル名の日付(「2025.12.02.HVC日報003.xlsx」→ 2025-12-02)。無ければ None。"""
    import re
    from datetime import date

    found = re.search(r"(20\d{2})[.\-_年](\d{1,2})[.\-_月](\d{1,2})", name)
    if not found:
        return None
    try:
        return date(*map(int, found.groups()))
    except ValueError:
        return None


def _date_notes(path: Path, head) -> list[str]:
    """ファイル名の日付とシートの作業日が違えば言う。**入れるのはシートの作業日。**

        昔のデータは日付変更前に保存したりで日付のつけ方がおかしいので分かりにくいです

    「2025.12.02.HVC日報.xlsx」の中身が 12/1(3直まで入れて翌朝に保存)、のように、
    ファイル名は保存した日で付いていることがあります。
    """
    from ..logic.shift import parse_business_date

    named = file_name_date(path.name)
    sheet = parse_business_date(getattr(head, "report_date", "") or "")
    if named is None or sheet is None or named == sheet:
        return []
    return [f"ファイル名の日付({named:%Y.%m.%d})とシートの作業日({head.report_date})が違います。"
            f"シートの作業日({head.report_date})として入れます"]


def _shrink_notes(repo: NippouRepository, pages: list[Page]) -> list[str]:
    """入れると**その日の枚数が減る**なら言う(古い保存のファイルで、新しいほうを上書きしそう)。

    直の間で行が移っても日の合計は変わらないので、日ごとに比べます。
    """
    def count(rows) -> float:
        total = 0.0
        for d in rows:
            try:
                total += float(str(d.con or "0").strip() or 0)
            except ValueError:
                pass
        return total

    out = []
    days = sorted({p.key[:2] for p in pages})
    for day, line in days:
        new = count(d for p in pages if p.key[:2] == (day, line) for d in p.details)
        shifts = {p.key[2] for p in pages if p.key[:2] == (day, line)}
        now = 0.0
        for shift in shifts:
            for page in repo.saved_pages(day, line, shift):
                loaded = repo.load(day, line, shift, page)
                if loaded:
                    now += count(loaded[1])
        if now > new:
            out.append(f"⚠ 入れると {day} の枚数が減ります(今 {now:g}枚 → このファイル {new:g}枚)。"
                       "同じ日の、もっと後に保存したファイルが既に入っていませんか")
    return out


def same_day_losers(paths: list[Path], line: str = "") -> dict[Path, str]:
    """一度に渡した xlsx のうち、**同じ作業日でほかに中身の多いファイルがある**もの → 理由。

    昔の日報は1日に何度も保存されていて(「…日報001」「…日報002」「…日報」)、
    途中で保存したほう(3直がまだ空)が後から入ると、全部入ったほうを上書きします。
    同じ作業日・同じラインのうち、枚数の合計 → 行数の多いほうだけを入れます。
    """
    from ..logic import nippou_sheet

    best: dict[tuple[str, str], tuple[tuple[float, int], Path]] = {}
    scores: dict[Path, tuple[tuple[str, str], tuple[float, int]]] = {}
    for path in paths:
        if not is_workbook(path):
            continue
        try:
            head = nippou_sheet.read_file_head(path)
            target = line_names.upgrade(line.strip()) or nippou_sheet.guess_line(head.line_text)
            parsed = nippou_sheet.parse_file(path, target or "?")
        except Exception:                         # noqa: BLE001 - 読めないものは本番で断る
            continue
        if not parsed.ok:
            continue
        total = 0.0
        for page in parsed.pages:
            for d in page.details:
                try:
                    total += float(str(d.con or "0").strip() or 0)
                except ValueError:
                    pass
        key = (head.report_date, target)
        score = (total, parsed.row_count)
        scores[path] = (key, score)
        if key not in best or score >= best[key][0]:   # 同じなら後のほう
            best[key] = (score, path)
    out = {}
    for path, (key, score) in scores.items():
        winner = best[key][1]
        if winner != path:
            out[path] = (f"同じ作業日({key[0]})のファイル「{winner.name}」のほうが中身が多い"
                         f"(このファイル {score[0]:g}枚 {score[1]}行 / あちら {best[key][0][0]:g}枚 "
                         f"{best[key][0][1]}行)ので、こちらは入れませんでした")
    return out


def _starts(repo: NippouRepository) -> dict[str, int]:
    """直の始まり。手元に写した「時間用」(`shift_config`)、読めなければ控え。"""
    from ..logic import nippou_sheet

    try:
        return nippou_sheet.shift_starts(repo.get_shift_times())
    except Exception:                             # noqa: BLE001 - 控えで読む
        return nippou_sheet.shift_starts()


def apply(repo: NippouRepository, path: Path, *, line: str = "",
          mark_synced: bool = True,
          out_dir: Optional[Path] = None) -> tuple[Preview, Result]:
    """取り込む。**下見と同じものを、もう一度読んでから入れます。**

    下見の結果を持ち回らずに読み直すのは、画面が持っている間にファイルが
    差し替わることがあるからです ── 見せたものと違うものを入れるより、
    読み直したほうが確かです(件数は結果に出ます)。

    進めるところは**保存(確定)と同じ**です ── 日報を入れ、その直の
    集計を作り直し、その日の集計CSVを書きます。

    **進み具合を置きながら進みます**(`nippou/job_progress.py`)。
    何百ページもあると何十秒もかかるので、画面が「止まっているのか
    動いているのか」を見に来られるようにするためです。呼ぶ側が
    `start()` していなければ、置く先が無いので何も起きません。
    """
    from . import summary
    from .. import job_progress
    from ..logic import progress as progress_logic

    base = _out_dir(out_dir)
    found = preview(repo, path, line, out_dir=base)   # 読む段も出します
    result = Result(marked_synced=mark_synced, csv_dir=str(base))
    if not found.ok:
        return found, result

    touched: set[tuple[str, str, str]] = set()
    job_progress.step(phase=progress_logic.PHASE_WRITE,
                      total=len(found.parsed.pages))
    for page, target in zip(found.parsed.pages, found.targets):
        job_progress.step(label=page.label)
        try:
            # 画面の外から書く(開いたままの古い画面に書き戻させない。page_writer)
            page_writer.save_page(repo, page.header, page.details, by_screen=False)
            if mark_synced:
                # **共有へは送らない。** 古いぶんは VBA の時代に入っている
                repo.mark_synced(page.key)
        except Exception as exc:                  # noqa: BLE001 - 1ページで止めない
            log.exception("取り込みに失敗しました: %s", page.label)
            result.failed.append((page.label, str(exc)))
            continue
        result.pages += 1
        result.rows += len(page.details)
        result.replaced += 1 if target.replaces else 0
        touched.add(page.key[:3])
        job_progress.step(done=result.pages + len(result.failed))

    # 前の取り込みで増えていたページを消す(その直は今度のページで足りる)
    for key in found.stale:
        try:
            if page_writer.delete_page(repo, *key):
                result.removed += 1
                touched.add(key[:3])
        except Exception as exc:                  # noqa: BLE001 - 1ページで止めない
            log.exception("古いページを消せませんでした: %s", key)
            result.failed.append((f"{key[0]} {key[2]} {key[3]}ページ(消す)", str(exc)))

    # **紙もグラフも集計から出ます。** 明細だけ入れて集計を作らないと、
    # 「記録を見る」には出るのにグラフには出ない、が起きます
    job_progress.step(phase=progress_logic.PHASE_SUMMARY,
                      total=len(touched))
    for done, key in enumerate(sorted(touched), start=1):
        job_progress.step(done=done, label=" ".join(key))
        try:
            if summary.refresh_shift(repo, *key) is not None:
                result.summaries += 1
        except Exception:                         # noqa: BLE001 - 日報は入っている
            log.exception("取り込んだ直の集計を作れませんでした: %s", key)

    days = sorted({key[:2] for key in touched})
    job_progress.step(phase=progress_logic.PHASE_CSV, total=len(days))
    _write_csv(repo, result, days, base)
    # 2つ目の出力先(決めてあれば)へは、**共有に入っている扱いの日報だけ**
    # (「そのままにする」、`services/second_output`)。「共有にも入れる」で
    # 入れたぶんは、あとで「共有へ保存」を押したときに出ます。呼ぶ側が出し先を名指ししたときは
    # そこだけ(試験や、別の場所へ出したいとき)
    if out_dir is None and mark_synced:
        _write_copies(repo, result, days, _extra_dirs(base))

    log.info("取り込み: %dページ %d行 (置き換え %d / 集計 %d / CSV %d日 %d本 / "
             "共有へ送らない=%s)",
             result.pages, result.rows, result.replaced, result.summaries,
             result.csv_days, result.csv_files, mark_synced)
    return found, result


def _write_csv(repo: NippouRepository, result: Result,
               days: list[tuple[str, str]], base: Path) -> None:
    """入れた日ぶんの集計CSVを書く。**保存(確定)と同じ3本・同じ置き場所。**

    出し先はその日報の**作業日**のフォルダ
    (`<出力先>/<ライン>/集計/yyyy.mm/dd`)です ──
    取り込んだ日のぶんなので、今日のフォルダではありません。

    **落ちても取り込みは成功のまま。** 出し先が共有で、そこへ届かない
    ことは普通にあります。日報が入らないほうが困るので、ここで握りつぶし、
    出せなかった日だけを結果に載せます。
    """
    from ..reporting import csv_export

    from .. import job_progress

    for done, (report_date, line) in enumerate(days, start=1):
        job_progress.step(done=done, label=f"{report_date} {line_names.label(line)}")
        try:
            written = csv_export.write_daily_set(repo, report_date, line, base)
        except Exception as exc:                  # noqa: BLE001 - 日報は入っている
            log.exception("取り込んだ日の集計CSVを出せませんでした: %s/%s",
                          report_date, line)
            result.csv_failed.append((f"{report_date} {line}", str(exc)))
            continue
        result.csv_days += 1
        result.csv_files += len(written.files)


def _extra_dirs(base: Path) -> list[Path]:
    """集計CSVの2つ目の出力先(`config.SETTINGS.summary_csv_dirs`)。"""
    from ..config import SETTINGS

    return [d for d in SETTINGS.summary_csv_dirs if d != base]


def _write_copies(repo: NippouRepository, result: Result,
                  days: list[tuple[str, str]], bases: list[Path]) -> None:
    """2つ目の出力先へ、入れた日ぶんの3本を同じ形で。

    **落ちても取り込みは成功のまま**(1つ目と同じ)。出せなかった日は
    「(2つ目)」を付けて結果に載せます ── どちらに出なかったのかが
    読めないと、見に行く先が分かりません。
    """
    from ..reporting import csv_export

    for base in bases:
        for report_date, line in days:
            copies = csv_export.write_copies(
                [base], lambda b: csv_export.write_daily_set(
                    repo, report_date, line, b).folder)
            for copy in copies:
                if not copy.ok:
                    result.csv_failed.append(
                        (f"{report_date} {line_names.label(line)}(2つ目 {base})", copy.error))


def write_template(path: Path) -> Path:
    """見本(見出しだけのCSV)を書く。**これに貼れば読めます。**"""
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8-sig", newline="") as f:
        f.write(csv_import.template_header() + "\r\n")
    return path


def default_template_path(base_dir: Path) -> Path:
    return base_dir / "取り込みの見本.csv"
