"""集計CSVの2つ目の出力先 ── **共有へ保存したときに出す**

    集計データの出力先を２つ設定したいです(使用、アクセス権の関係)
    月別の書き出し(自動集計)も2つ目に … あります
    ２つ目に追加した出力先に出すタイミングは共有保存を押したタイミングに

2つ目は**使う人とアクセス権の違う所**です(例: 管理の人だけが入れる
フォルダ)。そこに出すのは、打っている途中の日報ではなく**共有へ送った
日報**にします ── 保存(確定)のたびに出すと、共有にまだ無い中身が先に
2つ目へ届きます。

    出すとき                         出すもの
    「共有へ保存」で送れたとき        送った日の集計CSV 3本(日・ラインごと)
    月替わりの書き出し(共有へ保存の   月別の3本(`<2つ目>\\自動集計\\<ライン>\\yyyy.mm`)
      直後に走る/押して走らせる)
    過去の日報の取り込み              「そのままにする」(共有には入っている扱い)で
                                      入れた日の3本。「共有にも入れる」で入れた日は、
                                      あとで「共有へ保存」を押したときに出る

1つ目(集計CSV・印刷用HTMLの出力パス)は、これまでどおり保存のたびと
ボタンを押したときに出ます。

**2つ目に届かなくても、共有への保存は成功のまま**です(知らせに載せる)。

【まだ送れていない直がある日は、出さずに待つ】
CSVは**日ごと**(その日・そのラインの直ぜんぶ)です。共有へ保存が一部だけ
失敗して、同じ日に送れなかった直が残ると、その日のCSVには**まだ共有に
無い直**まで入ってしまいます。

    同じ日に、まだ共有へ送っていない直が別にあると、その直のぶんも
    2つ目に入ります … 直してください

そこで、送ったあとに**その日・そのラインにまだ未送信のページが残って
いれば、2つ目へは出しません**(知らせで言います)。残った直が次の
共有へ保存で送れたとき、その日の鍵が送れた側に入るので、そこで出ます。
月別の書き出しも同じで、その月に未送信のページが残っていれば待ちます。
2つ目には**共有に送り終わった中身だけ**が入ります。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

from ..config import SETTINGS
from ..db.repository import NippouRepository
from ..logging_setup import get_logger
from ..reporting import csv_export
from ..reporting.csv_export import Copy

log = get_logger("services.second_output")

#: 2つ目の下で、月別の書き出しを入れるフォルダ(VBA の「自動集計」と同じ名前)
MONTHLY_FOLDER = "自動集計"


def extra_dirs() -> list[Path]:
    """2つ目の出力先(決めていなければ空)。"""
    return SETTINGS.summary_csv_dirs[1:]


@dataclass
class Outcome:
    """2つ目へ出した結果。"""

    copies: list[Copy] = field(default_factory=list)
    #: まだ共有へ送れていない直があるので、**出さずに待っている**もの
    #: (「2026年9月24日 L-1」「2026年9月 のぶん」など)
    held: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        """出すべきものを全部出せたか(待っているもの・届かなかったものが無い)。"""
        return not self.held and all(c.ok for c in self.copies)

    def merge(self, other: "Outcome") -> "Outcome":
        """2つの結果を1つに(月替わりで何か月ぶんも出したとき)。"""
        return Outcome(copies=self.copies + other.copies,
                       held=self.held + other.held)

    def as_dict(self) -> dict[str, Any]:
        return {"copies": [c.as_dict() for c in self.copies], "held": self.held}

    def text(self, what: str) -> str:
        """知らせの行。**出せなかったもの・待っているものは名指しする**。"""
        parts = []
        ok = [c for c in self.copies if c.ok]
        if ok:
            where = sorted({str(c.base) for c in ok})
            parts.append(f"2つ目の出力先にも{what}を出しました({len(ok)}件): "
                         + "・".join(where))
        for c in self.copies:
            if not c.ok:
                parts.append(f"2つ目の出力先({c.base})に{what}を出せませんでした: {c.error}")
        if self.held:
            parts.append(f"2つ目の出力先へは、まだ送れていない直がある{what}を出していません"
                         f"({'・'.join(self.held)})── 次に共有へ保存で送れたときに出します")
        return "\n".join(parts)


def days_of(keys: Iterable[tuple]) -> list[tuple[str, str]]:
    """送ったページの鍵 `(日, ライン, 直, ページ)` から、`(日, ライン)` を並べる。"""
    return sorted({(str(k[0]), str(k[1])) for k in keys})


def unsent_days(repo: NippouRepository) -> set[tuple[str, str]]:
    """まだ共有へ送っていないページのある `(日, ライン)`。"""
    return {(h.report_date, h.line) for h in repo.pending_sync_headers()}


def after_push(repo: NippouRepository, pushed_keys: Iterable[tuple]) -> Outcome:
    """共有へ送れたページの日ぶんを、2つ目へ3本ずつ出す(年月のフォルダの説明2つも)。

    **送ったあとも未送信のページが残っている日は出さない**(上の説明)。
    """
    days = days_of(pushed_keys)
    if not extra_dirs() or not days:
        return Outcome()
    unsent = unsent_days(repo)
    ready = [d for d in days if d not in unsent]
    out = Outcome(held=[f"{d} {ln}" for d, ln in days if (d, ln) in unsent])
    for base in extra_dirs():
        for report_date, line in ready:
            out.copies += csv_export.write_copies(
                [base], lambda b, d=report_date, ln=line:
                csv_export.write_daily_set(repo, d, ln, b).folder)
    if out.held:
        log.info("2つ目の出力先へは待ちます(未送信の直がある日): %s",
                 ", ".join(out.held))
    return out


def monthly_base(base: Path) -> Path:
    return base / MONTHLY_FOLDER


def after_rollover(repo: NippouRepository, result) -> Outcome:
    """月替わりの書き出しを、2つ目の `自動集計` にも同じ月・同じ形で出す。

    1つ目で書けた月(`result.ran`)だけ。書けなかった(途中で終わった)
    ときは、2つ目も落ちた扱いにします。**その月に未送信のページが
    残っていれば待ちます**(押して走らせたときに起こりうる)。
    """
    if result is None or not getattr(result, "ran", False) or not extra_dirs():
        return Outcome()
    from . import month_rollover

    decision = result.decision
    if repo.month_has_pending(decision.year, decision.month):
        return Outcome(held=[f"{decision.year}年{decision.month}月のぶん"])

    def write(base: Path):
        other = month_rollover.run(repo, monthly_base(base),
                                   month=(decision.year, decision.month))
        if other.failed:
            raise RuntimeError("書き出しが途中で終わりました")
        return other.exports[0].folder.parent if other.exports else monthly_base(base)

    return Outcome(copies=csv_export.write_copies(extra_dirs(), write))
