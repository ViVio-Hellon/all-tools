"""人員フォーム（VBAの ``UF登録人員`` UserForm 相当）のコアロジック。

標準モジュールの ``人員()``（~4758行）/ ``GenerateOptionButtons``
（~21714行）/ ``FindCheckedCheckboxes``（~21809行）を、UI/Access依存を
排した純粋関数として移植したもの。提出された「班員名簿」テーブル
（管理番号/苗字/班/名前/読み/担当ライン）を前提にしている。
"""
from __future__ import annotations

from dataclasses import dataclass

# GenerateOptionButtons が個別の列(left座標)を割り当てていた班の並び
# （画面左から右へ A/B/C/D/昼/丸 の6列）。この6種類以外の値（空欄、
# 未定義の班名など）を持つ行は VBA と同じくスキップする
# (VBAの ``Case Else: '班が空欄や未定義の値...はスキップ'`` 相当)。
TEAM_ORDER: tuple[str, ...] = ("A", "B", "C", "D", "昼", "丸")


@dataclass(frozen=True)
class StaffMember:
    name: str
    team: str
    reading: str = ""
    #: 班員名簿の「オペレーター」(書いてあるまま。読み方は `logic/operator` ── v4.15.0)
    operator: str = ""


def group_by_team(members: list[StaffMember]) -> dict[str, list[str]]:
    """port of ``人員()`` + ``GenerateOptionButtons``。

    「読み」で五十音順ソートしてから「班」で安定ソートし（同じ班の中では
    読み順を維持する、VBAのバブルソートが安定ソートであることに由来）、
    既知の班ごとに名前のリストへグルーピングする。未知の班はスキップ。
    """
    ordered = sorted(members, key=lambda m: m.reading)
    ordered = sorted(ordered, key=lambda m: _team_sort_key(m.team))

    groups: dict[str, list[str]] = {team: [] for team in TEAM_ORDER}
    for member in ordered:
        if member.team in groups:
            groups[member.team].append(member.name)
    return groups


def _team_sort_key(team: str) -> int:
    try:
        return TEAM_ORDER.index(team)
    except ValueError:
        return len(TEAM_ORDER)  # 未知の班は末尾に(グルーピング時にどのみち除外される)


def split_worker(text: str) -> list[str]:
    """担当者欄の字を、名前ごとに分ける(半角・全角の空白どちらでも)。"""
    return [part for part in str(text or "").replace("　", " ").split(" ") if part]


def preselect(current: str, roster: list[str]) -> list[str]:
    """作業者を選ぶ窓を開いたとき、**最初からチェックしておく名前** (v4.7.0)。

    担当者欄にもう入っている名前のうち、名簿にあるもの。窓を開くたびに
    全部外れていると、1人足すだけでも全員を選び直すことになります。
    """
    known = set(roster)
    picked: list[str] = []
    for name in split_worker(current):
        if name in known and name not in picked:
            picked.append(name)
    return picked


def merge_worker(current: str, checked: list[str], roster: list[str],
                 trainee_suffix: str = "") -> str:
    """選び直した名前で担当者欄を作る。**名簿に無い字は消さない** (v4.7.0)。

    担当者欄には、名簿に無い字が入っていることがあります(手で打った名前・
    「新人教育」のような書き添え)。窓から選び直すたびにそれが消えると、
    **選んでいない字が黙って無くなります**。名簿に無いぶんは前のまま先頭に
    残し、そのあとに選んだ名前を並べます。
    """
    known = set(roster)
    kept = [name for name in split_worker(current) if name not in known]
    names = join_checked_names(list(checked), trainee_suffix)
    return " ".join(filter(None, [" ".join(kept), names]))


def join_checked_names(names: list[str], trainee_suffix: str = "") -> str:
    """port of ``FindCheckedCheckboxes``: チェック済みの名前をスペース
    区切りで連結する。``trainee_suffix`` を指定すると各名前に付記する
    （「新人教育」チェック相当。VBA側の正確な出力書式は元の ``UF登録人員``
    コードモジュールが今回の資料に含まれていなかったため、
    「名前(新人教育)」のような付記を合理的な実装として採用している）。
    """
    if trainee_suffix:
        names = [f"{name}{trainee_suffix}" for name in names]
    return " ".join(names)
