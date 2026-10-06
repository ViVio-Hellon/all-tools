"""直の引き継ぎ ── **前の直が片付かないまま、次の直が進むのを止める**

【止めたい流れ】

    共有へ保存を押し忘れる(または打ち間違いがあって通らない)
      → そのまま直の時間を過ぎる
      → 次の直の人が来て、そのまま入力を始める
      → 前の直は共有へ出ないまま、誰も直さない
      → 直せるのは管理者だけ(運用でそう決まっている)
      → 翌月の集計まで、誰も気づかない

途中のどこにも「気づく人」が居ません。**気づく人が必ず1人いる場所**は
1か所だけあります ── **次の直の始まり**です。そこに関門を置きます。

【関門は1つ、出口は2つ】
前の直が共有へ出ていないとき、次の直の人にできることは2つです:

    中身が綺麗(押し忘れだけ) … 「共有へ保存」を押す。**誰でも押せます**
                                ── 押すことは直すことではないので
    直すところが残っている   … **引き継ぐ**。誰が・いつ・何を残したまま
                                引き継いだかを記録して、次へ進む

**直させません。** 次の直の人に前の直を直させるのは運用上できません
(それができるなら、そもそもこの関門は要りません)。できるのは
「押す」か「引き継ぐ」かだけで、どちらも1押しです。

【なぜ止めてよいのか ── ラインは止まらない】
関門が立つのは**その直にまだ1ページも保存が無いあいだ**だけです。
打ち始めた人を途中で止めることはありません ── 打ちかけを抱えたまま
止められるのが、現場にとって一番悪い止まり方だからです。

そして出口は必ず開いています。1押しで通れるので、ラインが止まるのは
「画面を読んで、ボタンを1つ押す」あいだだけです。

【引き継いだあとも消えない】
引き継ぎは**片付けではありません。** 引き継いだ直は共有へ未送信のまま
なので、レールの数にも、入力画面の帯にも出続けます ── 消えるのは
共有へ出たときだけです。「引き継いだから、もう言われない」にすると、
1直の引き継ぎで丸1日隠れてしまいます。

【この層の約束】
ここは**純粋**。DBも時計も触らず、渡された事実だけで決めます。
事実を集めるのは `services/handover.py`、画面に出すのは `app/routes`。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional, Sequence

from .shift import about_shift
from . import line_names

#: 帯の主語。**画面の見出しに出る字**(`app/templates/entry.html`)
WHO = "前の直"

#: 帯の「これは何?」。**1行だけ出して、残りはマウスを載せたときに**
#: (`entry.html` の `.tip`)。
#:
#:     直せないまま引き継ぐ を押したわけでもないのに
#:     どんどん次いけるんだね ボタンの意味は？
#:
#: そう読めます。**止めるのと知らせるのは別**で、止まるのは「いまの直に
#: まだ1ページも保存が無いあいだ」だけです ── 1ページ保存したあとは
#: 押さなくても進めます(打ちかけを抱えた人を途中で止めないため)。
#: その約束を`decide()`の隣に置いておきます。ここが変わったら、
#: 画面の文も一緒に変わります。
WHY_LINE = "この帯は何を止めていて、2つのボタンは何をする?"
WHY_MORE = (
    "前の直が共有の日報管理へ出るまで、この帯は出たままです。"
    "入力を止めるのは、いまの直にまだ1ページも保存が無いあいだだけ ── "
    "1ページでも保存したあとは、どちらのボタンも押さずに続けられます"
    "(打ちかけを抱えた人を途中で止めないためです)。"
    "「共有へ保存する」は前の直を共有へ出します ── 押すことは直すこと"
    "ではないので誰でも押せます。これで消えます。"
    "「直せないまま引き継ぐ」は、直すところが残っているのを承知で先へ"
    "進むときに押します ── 誰が・いつ・何を残したまま引き継いだかを"
    "記録して、止まりを外します。片付けではないので、共有へ出るまで"
    "この帯は消えません。"
)

#: 引き継ぎの出口。**画面のボタンと1対1**
ACTION_PUSH = "push"        # 共有へ保存する(中身が綺麗なとき)
ACTION_TAKE = "take"        # 直せないまま引き継ぐ


@dataclass(frozen=True)
class Left:
    """片付いていない直1つぶん。**画面はこれを写すだけ。**"""

    report_date: str
    line: str
    shift: str
    pages: int = 0
    #: 直すところ。空なら「押し忘れだけ」
    problems: list[str] = field(default_factory=list)
    #: すでに引き継がれているなら、その人と時刻
    taken_by: str = ""
    taken_at: str = ""

    @property
    def key_text(self) -> str:
        return f"{self.report_date} {line_names.label(self.line)} {self.shift}"

    @property
    def clean(self) -> bool:
        """押し忘れだけ。**1押しで片付きます。**"""
        return not self.problems

    @property
    def taken(self) -> bool:
        return bool(self.taken_by)

    def as_dict(self) -> dict:
        return {"report_date": self.report_date, "line": self.line,
                "shift": self.shift, "pages": self.pages,
                "problems": list(self.problems), "clean": self.clean,
                "taken": self.taken, "taken_by": self.taken_by,
                "taken_at": self.taken_at, "key_text": self.key_text}


@dataclass(frozen=True)
class Gate:
    """関門の答え。**通してよいか、何をさせるか、何と言うか。**"""

    #: 止めるか。止めるのは「まだ1ページも打っていない直」のときだけ
    blocked: bool = False
    #: 片付いていない直(引き継ぎ済みも含む。数に出し続けるため)
    left: list[Left] = field(default_factory=list)
    message: str = ""
    #: 押せる出口。`ACTION_PUSH` / `ACTION_TAKE`
    actions: list[str] = field(default_factory=list)

    @property
    def any_left(self) -> bool:
        return bool(self.left)

    @property
    def about(self) -> str:
        """**この帯はどの直のことか。**

            上には赤文字でダメなところが書いてあって
            すぐ下には直すところはありませんって出てる

        赤い帯(ここ)は**前の直**のこと、その下の帯は**いまの直**の
        ことでした。主語が書いていなかったので、上下で言い合って
        いるように読めます。**帯に主語を付けます。**

        2直ぶん以上あるときは、古いほうを出して「ほか◯直」と足す
        ── 全部並べると主語のほうが長くなります。
        """
        if not self.left:
            return ""
        first = self.left[0]
        text = about_shift(first.report_date, first.shift)
        if len(self.left) > 1:
            text += f" ほか{len(self.left) - 1}直"
        return text

    @property
    def untaken(self) -> list[Left]:
        """**まだ誰も引き受けていない**もの。関門を立てるのはこれだけ。"""
        return [item for item in self.left if not item.taken]

    @property
    def all_clean(self) -> bool:
        """残っているものが全部「押し忘れだけ」。**1押しで消えます。**"""
        return bool(self.left) and all(item.clean for item in self.left)

    @property
    def problems(self) -> list[str]:
        """直せないものを、どの直のものか付けて並べる。"""
        found: list[str] = []
        for item in self.left:
            found.extend(f"{item.key_text} {p}" for p in item.problems)
        return found

    def as_dict(self) -> dict:
        return {"blocked": self.blocked, "any_left": self.any_left,
                "all_clean": self.all_clean, "message": self.message,
                "about": self.about, "who": WHO,
                "why_line": WHY_LINE, "why_more": WHY_MORE,
                "actions": list(self.actions),
                "problems": self.problems,
                "notes": [taken_note(item) for item in self.left
                          if item.taken],
                "left": [item.as_dict() for item in self.left]}


def decide(left: Sequence[Left], *, started: bool,
           read_only: bool = False) -> Gate:
    """関門を立てるか。

    止めるのは**3つがそろったときだけ**です:

        1. 共有へ出ていない直がある
        2. **そのどれもまだ引き継がれていない**
        3. いまの直にはまだ1ページも保存が無い(`started` が偽)

    2 は出口です ── 一度引き継げば通れます。引き継ぎは片付けでは
    ないので、知らせ(`message`)のほうは出し続けます。

    3 は「打ちかけを抱えたまま止めない」ためのものです。打ち始めた人を
    途中で止めるのが、現場にとって一番悪い止まり方だからです。

    `read_only` は過去の直を見ているとき。見るだけの画面に「前の直を
    片付けてください」と出しても、押す相手が違います。
    """
    if not left:
        return Gate()

    actions = [ACTION_PUSH]
    if any(not item.clean for item in left):
        actions.append(ACTION_TAKE)

    untaken = [item for item in left if not item.taken]
    blocked = bool(untaken) and not started and not read_only
    return Gate(blocked=blocked, left=list(left), actions=actions,
                message=_message(left, blocked=blocked))


def _message(left: Sequence[Left], *, blocked: bool) -> str:
    """関門の文。**何が残っていて、何を押せばよいか**まで書く。"""
    names = "、".join(item.key_text for item in left)
    head = (f"前の直が共有へ出ていません({len(left)}直ぶん): {names}"
            if len(left) > 1 else f"前の直({names})が共有へ出ていません")

    dirty = [item for item in left if not item.clean]
    lines = [head]
    if blocked:
        lines.append("片付けるまで、この直の入力は始められません。")
    if not dirty:
        # **主語を書く。** 「直すところはありません」だけだと、すぐ下の
        # 「いまの直」の帯と言い合っているように読めます
        lines.append("前の直に直すところはありません ── "
                     "「共有へ保存」を押せば済みます(誰でも押せます)。")
        return "\n".join(lines)

    count = sum(len(item.problems) for item in dirty)
    lines.append(f"直すところが {count}件 残っています。"
                 "前の直の間違いは、この画面からは直せません。")
    lines.append("「直せないまま引き継ぐ」を押すと、"
                 "引き継いだ人として記録したうえで入力を始められます。")
    lines.append("引き継いでも消えません ── "
                 "共有へ出るまで、この知らせは出続けます。")
    return "\n".join(lines)


def taken_note(item: Left) -> str:
    """引き継ぎ済みの直を1行で。帯に出し続けるための文。"""
    if not item.taken:
        return ""
    when = item.taken_at.replace("T", " ")[:16]
    who = item.taken_by
    if item.clean:
        return f"{item.key_text} を {who} が引き継ぎました({when})"
    return (f"{item.key_text} を {who} が引き継ぎました({when})。"
            f"直すところが {len(item.problems)}件 残っています ── "
            "管理者が直して共有へ出すまで消えません")


def summarize(findings: Sequence[str]) -> str:
    """引き継いだ時点の断りを、表へしまう1つの文字列にする。"""
    return "\n".join(str(f) for f in findings)


def worker_of(candidates: Sequence[Optional[str]]) -> str:
    """引き継いだ人の名前。**先に見つかったものを使う。**

    画面が送ってきた作業者 → いまの直に保存されている作業者、の順。
    どちらも空なら空のまま返します(名前を作りません)。
    """
    for value in candidates:
        text = str(value or "").strip()
        if text:
            return text
    return ""
