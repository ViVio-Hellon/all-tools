"""共有へ保存の**範囲** ── いまの直を送るのか、過去分を送るのか(v4.35.0)

    現在の直と前直は処理の違うボタンを設けたほうがいいと思うのですが...
    ・普段の共有へ送る は現在の直用
    ・エラー時に出ている 共有へ送る は 前直処理用

【何が困っていたか】
「共有へ保存」は1つで、押すと**未送信の直ぜんぶ**を確かめて、1つでも
引っかかれば**ぜんぶ止めて**いました。すると:

    1直: 共有に届かず送れない → 2直の人が「直せないまま引き継ぐ」で2直を始める
    2直の途中(18:00)で共有に届くようになる → 赤い帯の「共有へ保存する」を押す
    → 断られる。打っている途中の2直が「最終時間まで入っていない」「休憩が
      入っていない」に引っかかる(2直はまだ終わっていないので当たり前)
    → そのせいで1直も送れない

もう1つ、エラーのある直が1つあると、そのラインの後の直が**全部**共有へ
出ませんでした。

【ボタンを2つに分けます】

    前の直を送る は溜まったエラー過去直全部送れると思っていいですよね?
    ボタンには 前の直を送る ではなく 過去分を共有へ送る とかわかるようにしてください

赤い帯のボタンは **「過去分を共有へ送る」** です。1つ前の直だけでなく、
**いまの直ではない未送信の直ぜんぶ**(溜まった過去分すべて・何日前でも)を
1回で古い順に送ります。

    範囲        ボタン                         送るもの / 確かめるもの
    いまの直    入力画面のふだんの「共有へ保存」   いまの直だけ
    過去分      赤い帯の「過去分を共有へ送る」     いまの直**ではない**未送信の直ぜんぶ(古い順)

確かめる(関門)のも送る範囲だけです ── いまの直のボタンは前の直の
間違いに止められず、過去分のボタンはいまの直の途中に止められません。

【過去分が何直もあって、エラーが混ざっているとき】
**エラーの無い直は全部送り、エラーのある直は送らずに帯に残します**
(:func:`holds_one_by_one`)。残した直は管理者が直せば、同じボタンでもう一度
押すと送れます。押したあとの知らせには「送った直 n件 / エラーで残した直 m件」
と、残した直がどの直で何のエラーかを出します(全部送れたのか・何が残ったのかが
一目で分かるように)。

    ・関門(`services/shift_check`)は**直ごと**に7項目を見ています。直と直を
      またいで足す数(梱包数・休憩・作業時間)はありません ── 2直の間違いで
      1直の中身が変わることはないので、1直を止める理由がありません
    ・「1つでもあれば止める」は、ボタンが1つで**いまの直と前の直を一緒に**
      送っていた頃の決まりです。利用者も「仕方ない」とは言っていましたが、
      それで困ったのが今回の話です(エラーの直が1つあると後の直が全部
      出ない)。過去分のボタンでは、止めた直を名指しして帯に残せば、
      誰も気づかないまま残ることにはなりません
    ・送れた直は共有へ出て、止めた直は手元に未送信のまま残り、赤い帯にも
      レールの数にも出続けます(`logic/handover`)。直すのは今までどおり
      その直の人か管理者です

**いまの直のボタンは「1直ぶん」なので、止めるか送るかの2つだけ**です
(ページの一部だけ送ることはしません)。

【範囲を言わずに押したとき ── まとめて送る】
`scope` を添えない呼び方(前の版の画面・ほかの道具)は、今までどおり
**未送信ぜんぶを確かめて、1つでも引っかかれば止める**(:data:`SCOPE_ALL`)。
画面のボタンはどれも範囲を添えます。

【この層の約束】
ここは**純粋**。DBも時計も触らず、渡された鍵だけで決めます。
"""
from __future__ import annotations

from typing import Iterable, Optional, Sequence

from .aggregation import SHIFT_ORDER
from .shift import parse_business_date

#: いまの直だけ(入力画面のふだんの「共有へ保存」・残り5分の自動確定)
SCOPE_CURRENT = "current"
#: いまの直ではない未送信の直ぜんぶ = 過去分(赤い帯の「過去分を共有へ送る」・次の直の画面の拾い)
SCOPE_PREVIOUS = "previous"
#: 範囲を言わずに押した(前の版の画面・ほかの道具)。**未送信ぜんぶを、1つでも
#: 引っかかれば止める**(v4.34 までの「共有へ保存」と同じ)
SCOPE_ALL = "all"

SCOPES: tuple[str, ...] = (SCOPE_CURRENT, SCOPE_PREVIOUS, SCOPE_ALL)

#: 共有保存の履歴の「通し方」に書く字。**どちらのボタンで押したか**
VIA_TEXT = {SCOPE_CURRENT: "いまの直", SCOPE_PREVIOUS: "過去分", SCOPE_ALL: ""}

#: ボタンの名前。**画面の字と1対1**(`entry.html` / `settings.html`)
BUTTON_CURRENT = "共有へ保存"
BUTTON_PREVIOUS = "過去分を共有へ送る"

#: いまの直の断りに添える一文。**前の直は別のボタンで送れる**と言う
PREVIOUS_HINT = ("前の直(過去分)は、赤い帯の「過去分を共有へ送る」から送れます"
                 "(いまの直の途中には引っかかりません)")


def normalize(value: object) -> str:
    """画面が添えてきた範囲。知らない字・無しは :data:`SCOPE_ALL`(まとめて)。"""
    text = str(value or "").strip().lower()
    return text if text in SCOPES else SCOPE_ALL


def _order(key: tuple[str, str, str]) -> tuple:
    """古い順の並べ方 ── 報告日 → 直(集計と同じ並び `aggregation.SHIFT_ORDER`)→ ライン。

    日勤はふつう日勤だけのラインの直なので、同じ日の1〜3直と並ぶことはまず
    ありません(並んだときは集計の表と同じく3直のあと)。
    """
    report_date, line, shift = key
    day = parse_business_date(report_date)
    return (day.toordinal() if day else 0, SHIFT_ORDER.get(shift, 99), shift, line)


def oldest_first(keys: Iterable[tuple[str, str, str]]) -> list[tuple[str, str, str]]:
    """直の鍵を古い順に(同じ鍵は1つに)。"""
    unique: list[tuple[str, str, str]] = []
    for key in keys:
        key = tuple(key)
        if key not in unique:
            unique.append(key)
    return sorted(unique, key=_order)


def keys_in(scope: str, pending: Sequence[tuple[str, str, str]],
            current: Optional[tuple[str, str, str]]) -> list[tuple[str, str, str]]:
    """未送信の直(`pending`)のうち、**この範囲で送る直**を古い順に。

    いまの直が分からない(`current` が None)ときは、範囲を分けられないので
    まとめて送る(:data:`SCOPE_ALL`)と同じにします ── 押した人を止めない。
    """
    keys = oldest_first(pending)
    if current is None or scope == SCOPE_ALL:
        return keys
    current = tuple(current)
    if scope == SCOPE_CURRENT:
        return [k for k in keys if k == current]
    return [k for k in keys if k != current]


def holds_one_by_one(scope: str) -> bool:
    """引っかかった直だけを止めて、**ほかの直は送る**か。

    過去分の処理用(:data:`SCOPE_PREVIOUS`)だけです。いまの直は1直ぶんなので
    止めるか送るかで、まとめて送る(:data:`SCOPE_ALL`)は今までどおり
    1つでも引っかかれば止めます(モジュールの説明)。
    """
    return scope == SCOPE_PREVIOUS


def via_text(scope: str, *, skipped: bool = False, skip_phrase: str = "") -> str:
    """共有保存の履歴の「通し方」。**どちらのボタンで押したか**と、逃げ道の一文。

        いまの直 / 過去分 / いまの直・設備移動のため保存 / 設備移動のため保存(まとめて)
    """
    parts = [VIA_TEXT.get(scope, "")]
    if skipped and skip_phrase:
        parts.append(skip_phrase)
    return "・".join(p for p in parts if p)


def label(scope: str) -> str:
    """知らせの文に出す範囲の名前。まとめて送るときは空。"""
    return VIA_TEXT.get(scope, "")


def tally_text(sent: Sequence[str], held: Sequence[str]) -> str:
    """過去分を送ったあとの一行。**全部送れたのか・何が残ったのか**を一目で。

        過去分: 送った直 3件 / エラーで残した直 2件
    """
    return f"過去分: 送った直 {len(sent)}件 / エラーで残した直 {len(held)}件"
