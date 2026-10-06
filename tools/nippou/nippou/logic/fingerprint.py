"""指紋 ── **共有に在るものと、いま手元に在るものは同じか**

    共有へ保存 をしてから 保存(確定) を行うと
    共有へ未送信 1直ぶん になる
    差分のことを考えてだろうけども... 差分チェックすると重い？
    差分がなければやはり 共有へ保存 を何度も求める必要がないのでは

そのとおりでした。保存は**押すたびに**「まだ送っていない」(`dirty`)を
立てていたので、1文字も変えずに「保存(確定)」を押しただけで未送信へ
戻り、共有へ保存をもう一度求めていました。求められた人は押します ──
中身は同じなので、共有には同じものが書き直されるだけです。

【重くありません】
1ページは12行、共有へ出る欄は35ほど。文字を並べて SHA-256 を1回かける
だけで、桁は**マイクロ秒**です。同じ保存の中でやっている DELETE+INSERT
(13本)のほうが、比べものにならないほど重い ── 「差分チェックが重いから
毎回立てる」という取り引きは、そもそも成り立っていませんでした。

【何を数えるか ── 共有へ出る欄だけ】
数えるのは `access_bridge/sqlite_backend` の HEADER_COLUMNS /
DETAIL_COLUMNS と同じ範囲です。手元にしか無い欄(引当番号・行ごとの
理由)と、保存日時のような台帳は数えません:

    保存日時を数える  … 押すたびに指紋が変わる(いまと同じ)
    引当番号を数える  … 共有には同じものを送り直すだけ

行ごとの理由は、紙と共有へ出るときに1つにまとめて `header.reason` に
入ります(`logic/reasons.combine`)。そちらは数えるので、理由を直せば
指紋も変わります。

【空の行は数えません】
1文字も入っていない行は、共有へ出ても空です。12行そろえて送っていた
ものが1行になっても、共有から見た中身は変わりません ── 画面の都合で
指紋が動かないように、空の行は落としてから数えます。

【この層の約束】
ここは**純粋**。DBも時計も触らず、渡された値だけで指紋を作ります。
"""
from __future__ import annotations

import hashlib
from typing import Any, Iterable, Sequence

#: 共有へ出るヘッダーの欄(`sqlite_backend.HEADER_COLUMNS` と同じ範囲。
#: 鍵の4つ(報告日・ライン・直・ページ)は指紋の外 ── 鍵が違えば別の紙
#: なので、比べるまでもありません)
HEADER_FIELDS: tuple[str, ...] = (
    "worker", "day_shift", "count", "weight_kg",
    "lot_count", "coefficient_lot_count", "reason",
)

#: 共有へ出る明細の欄(`sqlite_backend.DETAIL_COLUMNS` と同じ範囲)。
#: **`hiki_no` と行ごとの `reason` は入っていません** ── どちらも手元
#: だけのもので、共有へは出ないためです
DETAIL_FIELDS: tuple[str, ...] = (
    "lot", "zai", "siz", "ken", "kz", "kh", "sz", "sh", "hit", "ai",
    "mai", "tut", "vc", "et", "s", "th", "ss", "ths", "sth", "tht",
    "con", "wei", "tim", "uni",
    "others1", "others2", "others3", "others4", "others5", "others6", "keisu",
)

#: 指紋の長さ。SHA-256 の頭128ビット ── 手元のDBの中で見分けるだけなので
#: 64文字は要りません(取り違えは 2^-64 の側)
_LENGTH = 32


def text(value: Any) -> str:
    """欄1つを文字にする。**None と空は同じ**、前後の空白は落とす。"""
    return "" if value is None else str(value).strip()


def digest(rows: Iterable[Sequence[Any]]) -> str:
    """値の並びから指紋を1つ。**並び順まで込みで**同じなら同じ。"""
    body = "\n".join("\t".join(text(cell) for cell in row) for row in rows)
    return hashlib.sha256(body.encode("utf-8")).hexdigest()[:_LENGTH]


def _row_values(record: Any, fields: Sequence[str]) -> list[str]:
    return [text(getattr(record, name, "")) for name in fields]


def page_fingerprint(header: Any, details: Sequence[Any]) -> str:
    """1ページぶんの指紋。**共有へ出る中身が同じなら、同じ指紋。**

    行は行番号の順に並べます ── 渡ってくる順に頼ると、同じ中身でも
    指紋が変わることがあります。
    """
    rows: list[Sequence[Any]] = [_row_values(header, HEADER_FIELDS)]
    ordered = sorted(details or (),
                     key=lambda d: int(getattr(d, "row_no", 0) or 0))
    for detail in ordered:
        values = _row_values(detail, DETAIL_FIELDS)
        if not any(values):
            continue                       # 空の行は共有から見て無いのと同じ
        rows.append([text(getattr(detail, "row_no", ""))] + values)
    return digest(rows)


#: 集計(`packing_report`)で数えない欄。**台帳と鍵**です
_SUMMARY_SKIP = frozenset({"id", "report_id", "dirty", "synced_at",
                           "created_at", "updated_at"})


def _record_values(record: Any) -> list[str]:
    """持っている欄をぜんぶ(台帳と、ぶら下がっている子をのぞいて)。

    子(明細の中の停止)を文字にすると、その子の `updated_at` まで混ざって
    **毎回違う指紋**になります。子は子で1行として数えます。
    """
    return [text(value) for name, value in vars(record).items()
            if name not in _SUMMARY_SKIP
            and not isinstance(value, (list, tuple, dict, set))]


def summary_fingerprint(report: Any, details: Sequence[Any] = ()) -> str:
    """1直ぶんの集計の指紋(親 → 明細 → その停止、の順)。

    集計は**打った日報からの投影**なので、日報が変わらなければこちらも
    変わりません。それでも別に数えるのは、投影の作り方(用途名や負荷係数
    のマスタ)が変わることがあるためです ── そのときは指紋が動いて、
    ちゃんと送り直しになります。
    """
    rows: list[Sequence[Any]] = [_record_values(report)]
    for detail in details or ():
        rows.append(_record_values(detail))
        for stop in getattr(detail, "stops", ()) or ():
            rows.append(_record_values(stop))
    return digest(rows)
