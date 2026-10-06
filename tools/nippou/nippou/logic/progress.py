"""いま、どこまで進んだか

    件数が多いときはプログレス表示させてください

何百ページもある過去データを取り込むあいだ、画面は「押した」まま
止まって見えます。**止まっているのか、動いているのか**が分からないのが
いちばん困ります ── 人は待てないのではなく、**待つ先が見えないと**
待てません(もう一度押す、閉じる、電源を切る、のどれかをします)。

【何を出すか】
数だけでは足りません。「120/340」と出ていても、**何を**しているのかが
分からなければ、あと何分かかるのかも見当が付きません。3つ出します:

    どの段か … 読んでいる / 入れている / 集計 / 集計CSV
    いくつ目か … 120/340ページ(割合も)
    いま何か … 2026年8月3日 L-1 1直 1ページ

【段を分ける理由】
取り込みは4つの仕事の積み重ねです。ページを入れ終わってからも
集計とCSVが残っていて、そこで数十秒かかることがあります ── 1本の棒で
「100%」にしてしまうと、**終わったはずなのに終わらない**ように見えます。

【この層の約束】
ここは**純粋**。DBも時計も触らず、数と名前から言葉を作るだけです。
進み具合を持つのは `nippou/job_progress.py`(プロセスに1つ)。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

#: 仕事の種類。**段の並びと、数える物の名前が仕事ごとに違う**
#:
#:     保存処理にもプログレスを表示し進捗がわかるようにしてください
#:
#: はじめは取り込みだけでした。共有へ保存も、直の数だけ確かめて・送って・
#: 集計CSVや履歴や標準作業時間を写して・月替わりを見る、と段が重なり、
#: 共有のフォルダが遠いと何秒もかかります。マスタの1行を直すのも、元の
#: ファイルへ書いてから写し直すので、共有が遅い日は待たされます。
JOB_IMPORT = "import"        # 過去の日報の取り込み
JOB_PUSH = "push"            # 共有へ保存
JOB_MASTER = "master"        # マスタの1行を直す / 足す / 消す

#: 段(そのまま画面の見出しになります)
PHASE_READ = "read"          # ファイルを読んでいる
PHASE_WRITE = "write"        # 日報を入れている
PHASE_SUMMARY = "summary"    # 直ごとの集計を作り直している
PHASE_CSV = "csv"            # 集計CSVを書いている
PHASE_DONE = "done"          # 終わり
# 共有へ保存
PHASE_CHECK = "check"        # 送る前の7項目(直ごと)
PHASE_SEND = "send"          # 共有へ送っている(ページごと)
PHASE_AFTER = "after"        # 2つ目の出力先・履歴・標準作業時間
PHASE_MONTH = "month"        # 月替わり
# マスタ
PHASE_MASTER_WRITE = "master_write"   # 元のファイルへ書いている
PHASE_RELOAD = "reload"               # 書いた内容を読み直している

#: 段の言い方。**画面にそのまま出る字**
PHASE_TEXT: dict[str, str] = {
    PHASE_READ: "ファイルを読んでいます",
    PHASE_WRITE: "日報を入れています",
    PHASE_SUMMARY: "集計を作り直しています",
    PHASE_CSV: "集計CSVを書いています",
    PHASE_DONE: "終わりました",
    PHASE_CHECK: "送る前に確かめています",
    PHASE_SEND: "共有へ送っています",
    PHASE_AFTER: "集計CSV・履歴・標準作業時間を写しています",
    PHASE_MONTH: "月替わりを確かめています",
    PHASE_MASTER_WRITE: "元のファイルに書いています",
    PHASE_RELOAD: "書いた内容を読み直しています",
}

#: 仕事ごとの段の並び(どこまで来たかを「3/4」で出すため)
JOB_PHASES: dict[str, tuple[str, ...]] = {
    JOB_IMPORT: (PHASE_READ, PHASE_WRITE, PHASE_SUMMARY, PHASE_CSV),
    JOB_PUSH: (PHASE_CHECK, PHASE_SEND, PHASE_AFTER, PHASE_MONTH),
    JOB_MASTER: (PHASE_MASTER_WRITE, PHASE_RELOAD),
}

#: 段がまだ決まっていないあいだの見出し
JOB_TITLE: dict[str, str] = {
    JOB_IMPORT: "取り込んでいます",
    JOB_PUSH: "共有へ保存しています",
    JOB_MASTER: "マスタに書いています",
}

#: 数える物の名前(「120/340ページ」)。段ごと
PHASE_UNIT: dict[str, str] = {
    PHASE_WRITE: "ページ",
    PHASE_CHECK: "直",
    PHASE_SEND: "ページ",
}

#: 段の並び(取り込み)。**前からある名前**なので残す
PHASE_ORDER: tuple[str, ...] = JOB_PHASES[JOB_IMPORT]


@dataclass(frozen=True)
class Progress:
    """進み具合ひとまとめ。**画面はこれを写すだけ。**"""

    #: 動いているか。偽なら画面は何も出しません
    running: bool = False
    #: 何の仕事か(`JOB_*`)。段の並びと見出しが変わる
    job: str = JOB_IMPORT
    phase: str = ""
    done: int = 0
    total: int = 0
    #: いま触っているもの(ページの名前・ファイルの名前)
    label: str = ""
    #: 何本目のファイルか(1本だけのときは 0)
    file_no: int = 0
    file_count: int = 0
    file_name: str = ""

    @property
    def percent(self) -> int:
        """0〜100。**総数が分からないうちは0**(嘘の100を出さない)。"""
        if self.total <= 0:
            return 0
        return max(0, min(100, round(self.done * 100 / self.total)))

    @property
    def phase_text(self) -> str:
        return PHASE_TEXT.get(self.phase, "")

    @property
    def phases(self) -> tuple[str, ...]:
        return JOB_PHASES.get(self.job, ())

    @property
    def step_no(self) -> int:
        """何段目か(1始まり)。並びに無い段は0。"""
        return (self.phases.index(self.phase) + 1
                if self.phase in self.phases else 0)

    @property
    def headline(self) -> str:
        """1行目。**段・いくつ目・割合**をこの順で。"""
        if not self.running:
            return ""
        head = self.phase_text or JOB_TITLE.get(self.job, "処理しています")
        if self.step_no:
            head = f"[{self.step_no}/{len(self.phases)}] {head}"
        if self.total > 0:
            head += f" {self.done}/{self.total}"
            head += PHASE_UNIT.get(self.phase, "")
            head += f" ({self.percent}%)"
        return head

    @property
    def title(self) -> str:
        """何の仕事か(枠の見出し)。"""
        return JOB_TITLE.get(self.job, "")

    @property
    def note(self) -> str:
        """2行目。**いま何を触っているか**(何本目かも添えて)。"""
        parts = []
        name = f"「{self.file_name}」" if self.file_name else ""
        if self.file_count > 1 and self.file_no:
            parts.append(f"{self.file_no}/{self.file_count}本目{name}")
        elif name:
            parts.append(name)
        if self.label:
            parts.append(self.label)
        return "  ".join(parts)

    def as_dict(self) -> dict[str, Any]:
        return {"running": self.running, "job": self.job,
                "title": self.title, "step_no": self.step_no,
                "steps": len(self.phases), "phase": self.phase,
                "done": self.done, "total": self.total,
                "percent": self.percent, "label": self.label,
                "headline": self.headline, "note": self.note,
                "file_no": self.file_no, "file_count": self.file_count,
                "file_name": self.file_name}


def idle() -> Progress:
    """動いていない。**画面は何も出しません。**"""
    return Progress()
