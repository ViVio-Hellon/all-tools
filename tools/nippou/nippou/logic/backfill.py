"""過去の直を後から作る(後日作成) ── **通すかどうかはここ1か所で決める**

    例えば前日の1直分を丸々打ち忘れた場合に、前日分を作成するには?

    過去分を作ることはある一定で仕方がない場面はあると思っています。
    ・管理者であること
    ・現在分とぶつかって整合性がとれなくならない
    ・抜けた部分に保存される
    ・後日作ったことがわかる

【前はどこからも作れなかった】
「記録を見る → 呼び出す」は**保存のある直しか開けません**(無ければ
「該当するデータがありません」)。前の直が空のときの知らせは「呼び出せば
その直のぶんとして保存できます」と案内していたのに、まさに丸ごと抜けた
直では行き止まりでした。

【決まり ── 4つの条件をそのまま関門にする】

    管理者であること     … 管理者モードでなければ断る
    現在分とぶつからない … **終わった直だけ**(いまの直・これからの直は
                           日報入力からふつうに打つ)。この端末がまだ
                           打てる直(固定した直の猶予中)も断る
    抜けた部分に保存     … 作るのは**空いている枠だけ**。直に保存が無ければ
                           1ページ目、あれば続きのページ(最後の次)。
                           **共有に入っているのに手元に無いページ**がある
                           (別のPCで打って送った)なら断る ── ここで作って
                           送ると、共有のそのページを上書きするため
    後日作ったことがわかる … 作った枠を `backfill` 表に残し、入力画面・
                           記録の一覧・紙・共有保存の履歴に「後日作成」と出す

【この層の約束】
ここは**純粋**。DBも時計も触らず、渡された事実だけで決めます。事実を
集めるのは `services/backfill.py`、画面に出すのは `app/routes`。
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Optional, Sequence
from . import line_names

#: 断る理由の種類(画面・試験が見分ける)
NOT_ADMIN = "not_admin"
BAD_KEY = "bad_key"
NOT_ENDED = "not_ended"
WRITING = "writing_now"
IN_SHARED = "in_shared"

#: 理由を書いてもらう欄の長さ
NOTE_MAX = 60


@dataclass(frozen=True)
class Decision:
    """後から作ってよいか。**画面はこれを写すだけ。**"""

    ok: bool
    page: int = 0
    code: str = ""
    message: str = ""
    #: 通すが、気をつけてほしいこと(共有を確かめられなかった など)
    caution: str = ""

    def as_dict(self) -> dict:
        return {"ok": self.ok, "page": self.page, "code": self.code,
                "message": self.message, "caution": self.caution}


def key_text(report_date: str, line: str, shift: str) -> str:
    return f"{report_date} {line_names.label(line)} {shift}"


def decide(*, admin: bool, report_date: str, line: str, shift: str,
           shift_end: Optional[datetime], now: datetime,
           local_pages: Sequence[int], shared_pages: Optional[Sequence[int]],
           writing: bool = False) -> Decision:
    """後から作るか。作るなら**どのページか**。

    `shift_end` はその直の終わりの時刻(報告日・直が読めなければ None)。
    `shared_pages` は共有の日報データに入っているページ(確かめられなければ
    None ── そのときは通して、気をつけることを添える)。
    `writing` はこの端末がまだその直を打てる(固定した直の猶予中)か。
    """
    what = key_text(report_date, line, shift)
    if not admin:
        return Decision(False, code=NOT_ADMIN, message=(
            "過去の直を後から作るには管理者モードが要ります"
            "(設定・管理者の「端末」で切り替えます)。"))
    if shift_end is None:
        return Decision(False, code=BAD_KEY, message=(
            f"報告日・直が読めません: {what}"))
    if now < shift_end:
        return Decision(False, code=NOT_ENDED, message=(
            f"{what} はまだ終わっていない直です(終わり {shift_end:%m/%d %H:%M})。"
            "いまの直・これからの直は、日報入力からふつうに打ってください。"))
    if writing:
        return Decision(False, code=WRITING, message=(
            f"{what} は、この端末でまだ打てる直です。日報入力から続けてください。"))
    local = sorted({int(p) for p in local_pages})
    if shared_pages is not None:
        elsewhere = sorted({int(p) for p in shared_pages} - set(local))
        if elsewhere:
            pages = "・".join(str(p) for p in elsewhere)
            return Decision(False, code=IN_SHARED, message=(
                f"{what} は、共有の日報データにページ{pages}が入っています"
                "(別のPCで打って送ったもの)。ここで作って送ると、共有のその"
                "ページを上書きします。打ったPCの「記録を見る」から呼び出して"
                "直してください。"))
    page = (max(local) + 1) if local else 1
    if local:
        done = "・".join(str(p) for p in local)
        head = (f"{what} の第{page}ページを後から作ります"
                f"(第{done}ページは保存済み。続きのページとして足します)。")
    else:
        head = (f"{what} には保存が1ページもありません。第1ページを後から作ります。")
    caution = "" if shared_pages is not None else (
        "共有の日報データを確かめられませんでした(Access か、届かない)。"
        "別のPCでこの直を打っていないことを確かめてから進めてください。")
    return Decision(True, page=page, message=head, caution=caution)


def clean_note(text: str) -> str:
    """理由の欄。**1行・短く**(紙と一覧に出す)。"""
    return " ".join(str(text or "").split())[:NOTE_MAX]


def stamp_text(info: dict) -> str:
    """「後日作成」の印の字。**いつ・どこで・なぜ**を1行で。"""
    opened = str(info.get("opened_at", "") or "")
    when = opened[:16].replace("T", " ").replace("-", "/") if opened else ""
    parts = [p for p in (when, str(info.get("terminal", "") or "")) if p]
    text = "後日作成" + (f"({' '.join(parts)})" if parts else "")
    note = str(info.get("note", "") or "")
    return f"{text} 理由: {note}" if note else text
